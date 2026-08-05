"""
批量把 tb_archive 里的中文档案（lang=0）翻译成英文，作为新记录插回 tb_archive（lang=1）。

用法（必须在 repo 根目录跑）：
    python archive_content_markdown_update/translate_en.py --limit 5      # 先试跑 5 条
    python archive_content_markdown_update/translate_en.py --ids 2335,2333
    python archive_content_markdown_update/translate_en.py                # 跑完全部
    python archive_content_markdown_update/translate_en.py --workers 8    # 加大并发

单条失败（AI 输出不合法 / 入库报错）只跳过它自己，记进 data/translate_en_failed.tsv，
整批继续。已翻译的源 id 记进 data/translate_en_done.txt，重跑自动跳过（断点续跑）。
"""

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from openai import OpenAI
from dotenv import dotenv_values

from export import export_archive_data
from import_archive import connect, execute_sql, generate_sql
from polish import clean_json_str, try_auto_repair, make_client, PROVIDERS, DEFAULT_PROVIDER

# ── 配置 ──────────────────────────────────────────────────────
MODEL = "deepseek-v4-flash"          # DeepSeek Flash
MAX_TOKENS = 65536
TEMPERATURE = 0.3                    # 翻译任务，比润色更收敛

DATA_DIR = "data"
DONE_FILE = os.path.join(DATA_DIR, "translate_en_done.txt")     # 已翻译的源 id
FAILED_FILE = os.path.join(DATA_DIR, "translate_en_failed.tsv") # 失败清单

TARGET_LANG = 1                      # 见 intl.md：1 = 英文
DEFAULT_WORKERS = 4                  # 并发翻译的线程数（--workers 覆盖）
MAX_ATTEMPTS = 2                     # 空响应/解析失败多半是偶发，重试一次再判死

# 不送给 AI 的字段：库侧元数据，翻译无关（id / 时间戳另由 import_archive 拦着不写库）
PAYLOAD_SKIP_FIELDS = {"id", "created_at", "updated_at", "deleted_at", "view_count"}


SYSTEM_PROMPT = """You are a professional translator working on an archive of anomalous
phenomena and true-crime case files. You will receive one archive record as JSON.

Translate it from Chinese into natural, fluent, publication-quality English, and return
the same JSON object with the Chinese prose replaced by English.

## Translate
`title`, `content`, `location_desc`, `tags`, the `title` of each `ref_links` entry, and
every human-readable string inside `characters` / `timelines` / `evidence`
(`name`, `role`, `description`, `base_relation`, `action`, `detail`, `time_display`,
the `source` of an evidence node, etc.).

## Keep byte-identical
- Every key name, and the order and length of every array.
- All node/edge ids and the references between them: `id`, `source`, `target`,
  `related_characters`, `related_timelines` (values like "u1", "t3", "e2").
  Note `source`/`target` on an edge are ids — never translate those.
- All enum values: `time_type`, `importance`, `reliability`, `relation_type`,
  `type`, `status`, `is_private`, `lang`.
- All timestamps (`timestamp`, `occurred_at`, `closed_at`), numbers, nulls, booleans,
  `author_id`, `case_id`, `location`.
- Every URL, including `ref_links[].url` and any link inside `content`.

## content rules
- `content` is markdown. Keep the structure exactly: same heading levels, same
  paragraph breaks, same bold/italic emphasis.
- Keep any image line `![alt](url)` and any video line `@[video](url)` where it is.
  Translate the alt text, never the URL.
- Translate the whole body. Do not summarize, shorten, or omit any paragraph.
- Chinese proper nouns: use the standard English name where one exists
  (e.g. 广州塔 → Canton Tower, 西湖 → West Lake), otherwise pinyin with a short
  gloss on first use.

## Output
- Return the complete JSON object and nothing else: no explanation, no markdown
  code fence, no preamble.
- The JSON must be valid and parseable. Escape newlines inside strings as \\n.
"""


class TranslateFailed(RuntimeError):
    """这一条翻译不可用，跳过它自己"""


class BalanceExhausted(RuntimeError):
    """API 余额不足，继续跑只会把剩下的记录全烧成失败——直接中止整批"""


def is_balance_error(exc: Exception) -> bool:
    """识别余额不足（DeepSeek 返回 402 Insufficient Balance）"""
    if getattr(exc, "status_code", None) == 402:
        return True
    msg = str(exc).lower()
    return "insufficient balance" in msg or ("402" in msg and "balance" in msg)


def translate_record(src: dict, client: OpenAI) -> dict:
    """调 DeepSeek Flash 翻译一条，返回可直接入库的新档案 dict"""
    payload = {k: v for k, v in src.items() if k not in PAYLOAD_SKIP_FIELDS}

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",
                 "content": json.dumps(payload, ensure_ascii=False, indent=2)},
            ],
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
        )
    except Exception as e:
        if is_balance_error(e):
            raise BalanceExhausted(str(e)) from e
        raise TranslateFailed(f"API 调用失败：{e}") from e

    choice = resp.choices[0]
    if choice.finish_reason == "length":
        # 截断的 JSON 就算能修复也是残缺正文，不能入库
        raise TranslateFailed("输出被截断（max_tokens），译文不完整")

    raw = choice.message.content or ""
    if not raw.strip():
        raise TranslateFailed("AI 返回空内容")

    cleaned = clean_json_str(raw)
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError as e:
        result, how = try_auto_repair(cleaned)
        if result is None:
            raise TranslateFailed(f"JSON 解析失败：{e}") from e
        print(f"  🔧 自动修复成功（{how}）")

    if not isinstance(result, dict):
        raise TranslateFailed("AI 返回的不是 JSON 对象")
    if not result.get("title") or not result.get("content"):
        raise TranslateFailed("译文缺 title 或 content")
    if result["title"] == src.get("title"):
        raise TranslateFailed("title 没被翻译，疑似 AI 原样返回")

    result["lang"] = TARGET_LANG
    result["view_count"] = 0
    for field in ("id", "created_at", "updated_at", "deleted_at"):
        result.pop(field, None)

    return result


# ── 断点续跑账本 ──────────────────────────────────────────────
# 账本和终端输出都被多个 worker 共用，加锁避免写串行、打印互相插队
_io_lock = threading.Lock()


def load_done() -> set:
    if not os.path.exists(DONE_FILE):
        return set()
    with open(DONE_FILE, "r", encoding="utf-8") as f:
        return {int(line.split("\t")[0]) for line in f if line.strip()}


def mark_done(src_id: int, new_title: str):
    with _io_lock, open(DONE_FILE, "a", encoding="utf-8") as f:
        f.write(f"{src_id}\t{new_title}\n")


def mark_failed(src_id: int, title: str, reason):
    reason = str(reason).replace("\t", " ").replace("\n", " ")[:500]
    with _io_lock:
        exists = os.path.exists(FAILED_FILE)
        with open(FAILED_FILE, "a", encoding="utf-8") as f:
            if not exists:
                f.write("time\tid\ttitle\treason\n")
            f.write(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{src_id}\t{title}\t{reason}\n"
            )


def log(lines: list):
    """一条记录的输出整块打印，免得并发下几条记录的行交织在一起"""
    with _io_lock:
        print("\n".join(lines), flush=True)


def list_pending(config: dict, done: set, only_ids) -> list:
    """待翻译的中文档案：lang=0 且不在账本里"""
    conn = connect(config)
    try:
        with conn.cursor() as cur:
            if only_ids:
                cur.execute(
                    "SELECT id, title FROM tb_archive "
                    "WHERE id = ANY(%s) AND deleted_at IS NULL ORDER BY id DESC",
                    (only_ids,),
                )
            else:
                cur.execute(
                    "SELECT id, title FROM tb_archive "
                    "WHERE lang = 0 AND deleted_at IS NULL ORDER BY id DESC"
                )
            rows = cur.fetchall()
    finally:
        conn.close()

    return [(rid, title) for rid, title in rows if rid not in done]


# ── 单条处理 ──────────────────────────────────────────────────
def process_one(src_id: int, src_title: str, client: OpenAI,
                config: dict, abort: threading.Event) -> str:
    """
    翻译并入库一条，返回 "ok" / "failed" / "abort"。
    只在这里访问网络和数据库；每个 worker 各自建连接，互不共用。
    """
    if abort.is_set():
        return "abort"

    head = f"[id={src_id}] {src_title[:40]}"

    try:
        src = export_archive_data(src_id, config)
    except Exception as e:
        log([f"{head}\n  ❌ 读取失败：{e}"])
        mark_failed(src_id, src_title, f"读取失败: {e}")
        return "failed"

    started = time.time()
    translated = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            translated = translate_record(src, client)
            break
        except BalanceExhausted as e:
            # 余额不足不是「这一条的问题」，继续跑只会把剩下的全烧成失败
            abort.set()
            log([f"{head}\n  💳 API 余额不足，中止本次运行：{e}"])
            return "abort"
        except Exception as e:
            if attempt < MAX_ATTEMPTS and not abort.is_set():
                log([f"{head}\n  🔁 第 {attempt} 次失败（{e}），重试"])
                continue
            log([f"{head}\n  ⏭️  跳过：{e}"])
            mark_failed(src_id, src_title, e)
            return "failed"

    elapsed = time.time() - started
    if translated is None:            # 理论到不了，兜住类型
        return "failed"

    try:
        execute_sql(generate_sql(translated), config)
    except Exception as e:
        log([f"{head}\n  ✅ 已翻译（{elapsed:.0f}s）\n  ❌ 入库失败（已回滚）：{e}"])
        mark_failed(src_id, src_title, f"入库失败: {e}")
        return "failed"

    mark_done(src_id, translated["title"])
    log([
        head,
        f"  ✅ 已翻译（{elapsed:.0f}s）：{translated['title'][:60]}",
        f"  🗄️  已入库",
    ])
    return "ok"


# ── 主流程 ────────────────────────────────────────────────────
def run(args):
    config = dotenv_values(".env")
    os.makedirs(DATA_DIR, exist_ok=True)

    client = make_client(config, args.provider)

    done = load_done()
    only_ids = [int(x) for x in args.ids.split(",")] if args.ids else None
    pending = list_pending(config, done, only_ids)

    if args.limit:
        pending = pending[: args.limit]

    if not pending:
        print("📭 没有待翻译的记录")
        return

    workers = max(1, args.workers)
    print(f"📚 待翻译 {len(pending)} 条（账本里已完成 {len(done)} 条）")
    print(f"🤖 模型：{MODEL}，并发 {workers}，provider = {args.provider}"
          f"（{PROVIDERS[args.provider]['label']}）")
    print("─" * 56)

    abort = threading.Event()
    ok = failed = aborted = 0
    started_all = time.time()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(process_one, rid, title, client, config, abort): rid
            for rid, title in pending
        }

        for n, future in enumerate(as_completed(futures), 1):
            result = future.result()
            if result == "ok":
                ok += 1
            elif result == "failed":
                failed += 1
            else:
                aborted += 1

            done_n = ok + failed
            if done_n and done_n % 20 == 0:
                rate = (time.time() - started_all) / done_n
                left = (len(pending) - n) * rate / max(1, workers)
                log([f"  📈 进度 {n}/{len(pending)}（成功 {ok} 失败 {failed}），"
                     f"预计剩余 {left / 3600:.1f} 小时"])

    print("\n" + "─" * 56)
    print(f"🏁 完成：成功 {ok} 条，失败 {failed} 条，"
          f"耗时 {(time.time() - started_all) / 60:.1f} 分钟")
    if aborted:
        print(f"   💳 余额不足中止，{aborted} 条未处理；充值后重跑即可，"
              f"已完成的不会重复翻译")
    if failed:
        print(f"   失败清单：{FAILED_FILE}")


def main():
    parser = argparse.ArgumentParser(
        description="批量把 tb_archive 的中文档案翻译成英文并插回"
    )
    parser.add_argument("--limit", type=int, help="本次最多处理多少条")
    parser.add_argument("--ids", help="只处理指定 id，逗号分隔")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"并发翻译的线程数（默认 {DEFAULT_WORKERS}）")
    parser.add_argument("--provider", choices=list(PROVIDERS.keys()),
                        default=DEFAULT_PROVIDER,
                        help=f"API provider（默认 {DEFAULT_PROVIDER}）")
    args = parser.parse_args()

    try:
        run(args)
    except KeyboardInterrupt:
        print("\n👋 已中断")
        sys.exit(0)


if __name__ == "__main__":
    main()
