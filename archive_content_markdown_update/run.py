import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from dotenv import dotenv_values
from html.parser import HTMLParser
from openai import OpenAI

from polish import polish_data, AIRejected, ParseFailed, drain_keys
from polish import make_client, select_provider_interactive, PROVIDERS, DEFAULT_PROVIDER
from image_search import find_cover
from import_archive import generate_sql, execute_sql

config = dotenv_values(".env")
DATA_DIR = "data"
FILES_TO_SQL_DIR = "FILES_TO_SQL"

# 额外的输入目录：这些目录里的文件**只读**，即使 DELETE_AFTER_IMPORT=True 也不会删源文件
EXTRA_INPUT_DIRS = ("etc/truecrime_json_convert",)
MIN_CONTENT_LEN = 200    # 正文短于此字数的记录直接舍弃，不送 AI
FAILURE_DIR = "failure"  # 解析失败的 AI 原始输出存放处
JOBS_DONE_FILE = "jobs_done.txt"   # 已完成的 content_id 流水，每行一条
ENABLE_IMAGE_SEARCH = True         # 是否给档案配百度图片
ENABLE_DB_IMPORT = True            # 每生成一条就直接写入数据库
DELETE_AFTER_IMPORT = True         # 文件内所有记录都成功入库后，删除 FILES_TO_SQL 里的源文件

# ── 禁跑时段（北京时间）──────────────────────────────────────
# 落在这些区间内就挂起，区间外正常跑。左闭右开：12:00、18:00 整点即恢复。
ENABLE_PAUSE_WINDOW = True
BEIJING_TZ = timezone(timedelta(hours=8))
PAUSE_WINDOWS = ((9, 12), (14, 18))
WAIT_TICK = 60                     # 等待时的轮询间隔（秒）
WAIT_HEARTBEAT = 600               # 每隔多久打印一次剩余时间（秒）
FILE_POLL_TICK = 10                # 目录空时多久扫一次新文件（秒）

# --force 启动后置 True，无视禁跑时段强制跑（每条开跑前仍打印一次提示）
FORCE_BYPASS_PAUSE = False

session_sql_path = ""              # 本次运行的 SQL 存档，run() 里按时间戳初始化


class _TextExtractor(HTMLParser):
    """从 HTML 提取纯文本，保留段落换行"""

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True
        if tag in ("p", "div", "br", "h1", "h2", "h3", "h4", "h5", "li", "tr"):
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts).strip()


def html_to_text(html_str: str) -> str:
    parser = _TextExtractor()
    parser.feed(html_str)
    return parser.get_text()


def zhihu_to_archive(item: dict) -> dict:
    """知乎抓取条目 → 档案 dict，只保留 tb_archive 有对应列的字段"""
    ref_links = []
    if item.get("content_url"):
        ref_links.append({
            "title": f"知乎：{item.get('title') or '原文'}",
            "url": item["content_url"],
        })

    return {
        "title": item.get("title") or "无标题",
        "content": item.get("content_text") or "",
        # desc 只喂给 AI 做主题前置判断，不入库（见 import_archive.SKIP_FIELDS）
        "desc": item.get("desc") or None,
        # content_id 用于 jobs_done.txt 追溯，既不喂 AI 也不入库
        "content_id": item.get("content_id"),
        "ref_links": ref_links or None,
    }


def truecrime_to_archive(item: dict) -> dict:
    """truecrime 抓取条目（etc/truecrime_json_convert/truecrime.json）→ 档案 dict。
    字段映射参照 etc/truecrime_json_convert/truecrime_converter.py：
      - content 是 list[str]（每项一段），按段落 join
      - title 缺失时回退到 main_character_name
      - ref_links 用 AI prompt 期望的 {title, url} 形态拼出
        （原 converter 的 {type, label, description} 不会被 polish.py 利用）
    """
    raw_content = item.get("content") or []
    if isinstance(raw_content, list):
        content = "\n\n".join(
            str(s).strip() for s in raw_content if str(s).strip()
        )
    else:
        content = str(raw_content or "")

    title = (
        item.get("title")
        or item.get("main_character_name")
        or "无标题"
    )

    source_label = item.get("source") or "Original Article"

    ref_links: list[dict] = []
    if item.get("org_url"):
        ref_links.append({
            "title": f"{source_label}：{title}",
            "url": item["org_url"],
        })
    for img in (item.get("img_urls_captions") or []):
        if isinstance(img, dict) and img.get("url"):
            ref_links.append({
                "title": img.get("caption") or "Case image",
                "url": img["url"],
            })
    for vid_url in (item.get("yt_video_urls") or []):
        if vid_url:
            ref_links.append({
                "title": "Related video",
                "url": vid_url,
            })

    # desc 给 AI 做主题前置判断；真实案件来源，确保不被前置过滤误杀
    desc = f"{source_label} 真实案件档案：{title}"

    return {
        "title": title,
        "content": content,
        # desc 只喂给 AI，不入库（import_archive.SKIP_FIELDS 已拦）
        "desc": desc,
        # content_id 用 org_url 做追溯键（jobs_done.txt 流水）；原 converter 的 created_at 不在此使用
        "content_id": item.get("org_url"),
        "ref_links": ref_links or None,
    }


def normalize_item(item: dict, fallback_title: str) -> dict:
    """把单条 JSON 记录归一化成含 title / content 的档案 dict"""
    if not isinstance(item, dict):
        raise ValueError(f"JSON 记录不是对象：{type(item).__name__}")

    # 知乎抓取格式：正文在 content_text
    if "content" not in item and "content_text" in item:
        item = zhihu_to_archive(item)
    # truecrime 抓取格式：content 是 list[str]，且带 org_url/source 等特征字段
    elif (
        isinstance(item.get("content"), list)
        and (item.get("org_url") or item.get("img_urls_captions") is not None)
    ):
        item = truecrime_to_archive(item)

    if "content" not in item:
        raise ValueError("JSON 记录缺少 'content' 字段")

    # 太短的正文撑不起一份档案，直接丢弃，省掉一次 AI 调用
    length = len(item["content"] or "")
    if length < MIN_CONTENT_LEN:
        raise ValueError(f"正文仅 {length} 字，不足 {MIN_CONTENT_LEN} 字")

    if not item.get("title"):
        item["title"] = fallback_title

    return item


def parse_file_to_dicts(file_path: str) -> list[dict]:
    """将 HTML / TXT / JSON 文件解析为档案 dict 列表（JSON 数组 → 多份档案）"""
    ext = os.path.splitext(file_path)[1].lower()
    stem = os.path.splitext(os.path.basename(file_path))[0]

    if ext in (".txt", ".md"):
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        # 字幕类 txt 正文里往往没有事件名，把文件名补到首行给 AI 当标题线索
        if content.lstrip().split("\n", 1)[0].strip() != stem:
            content = f"{stem}\n\n{content.lstrip()}"
        return [{"title": stem, "content": content}]

    if ext == ".html":
        with open(file_path, "r", encoding="utf-8") as f:
            html_str = f.read()
        return [{"title": stem, "content": html_to_text(html_str)}]

    if ext == ".json":
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            if not data:
                raise ValueError("JSON 数组为空")
            records = []
            for i, item in enumerate(data, 1):
                try:
                    records.append(normalize_item(item, f"{stem}_{i:02d}"))
                except ValueError as e:
                    print(f"  ⚠️  第 {i} 条跳过：{e}")
            if not records:
                raise ValueError("数组中没有可用记录")
            return records

        return [normalize_item(data, stem)]

    raise ValueError(f"不支持的文件类型：{ext}")




def insert_cover_image(content: str, url: str, alt: str) -> str:
    """把配图插到第一个正文段落之后（跳过开头的标题行）"""
    blocks = content.split("\n\n")
    img_md = f"![{alt}]({url})"

    for i, block in enumerate(blocks):
        stripped = block.strip()
        # 空块、标题、引用、列表都不算「段落」，继续往下找
        if not stripped or stripped.startswith(("#", ">", "-", "*", "|", "!", "[")):
            continue
        blocks.insert(i + 1, img_md)
        return "\n\n".join(blocks)

    # 整篇都没有普通段落（极短或全是标题）：附到末尾
    return content.rstrip() + "\n\n" + img_md


def attach_cover_image(data: dict):
    """按润色后的 title 找配图并插入 content，失败只警告不中断"""
    if not ENABLE_IMAGE_SEARCH:
        return

    title = data.get("title")
    content = data.get("content")
    if not title or not content:
        return

    url = find_cover(title)
    if not url:
        print(f"  🖼️  未找到「{title}」的配图，跳过配图")
        return

    data["content"] = insert_cover_image(content, url, title)
    print(f"  🖼️  已插入配图：{url[:70]}...")


def record_done(data: dict, stem: str):
    """AI 处理完一条就往 jobs_done.txt 追加一行，供后续追溯/去重"""
    marker = data.get("content_id") or stem
    with open(JOBS_DONE_FILE, "a", encoding="utf-8") as f:
        f.write(f"{marker}\n")


def beijing_now() -> datetime:
    return datetime.now(BEIJING_TZ)


def should_pause(now: datetime) -> bool:
    """当前是否落在禁跑时段内（左闭右开）"""
    return any(start <= now.hour < end for start, end in PAUSE_WINDOWS)


def next_resume_time(now: datetime) -> datetime:
    """当前所处禁跑时段的结束时刻，也就是可以恢复的时间"""
    for start, end in PAUSE_WINDOWS:
        if start <= now.hour < end:
            if end >= 24:      # 跨天的禁跑段，恢复点是次日 0 点
                return (now + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
            return now.replace(hour=end, minute=0, second=0, microsecond=0)

    return now                 # 本来就不在禁跑段


def wait_out_pause_window():
    """落在禁跑时段就挂起，直到该时段结束；FORCE_BYPASS_PAUSE=True 时直接放行"""
    if not ENABLE_PAUSE_WINDOW:
        return

    now = beijing_now()
    if not should_pause(now):
        return

    if FORCE_BYPASS_PAUSE:
        print(f"\n  ⚠️  --force：当前 {now:%H:%M} 处于禁跑时段，无视规则继续")
        return

    target = next_resume_time(now)
    windows = "、".join(f"{s}:00-{e}:00" for s, e in PAUSE_WINDOWS)
    print(f"\n  ⏸  当前北京时间 {now:%Y-%m-%d %H:%M:%S} 处于禁跑时段（{windows}）")
    print(f"     暂停解析，将于 {target:%Y-%m-%d %H:%M} 恢复"
          f"（约 {(target - now).total_seconds() / 3600:.1f} 小时后）")

    last_beat = time.monotonic()
    while True:
        now = beijing_now()
        if not should_pause(now):
            break

        remain = (next_resume_time(now) - now).total_seconds()
        if remain <= 0:
            break

        time.sleep(min(WAIT_TICK, remain))

        if time.monotonic() - last_beat >= WAIT_HEARTBEAT:
            last_beat = time.monotonic()
            now = beijing_now()
            left = (next_resume_time(now) - now).total_seconds()
            print(f"     ⏳ 仍在暂停，剩余约 {left / 60:.0f} 分钟")

    print(f"  ▶️  北京时间 {beijing_now():%H:%M:%S}，离开禁跑时段，继续解析\n")


def is_balance_error(exc: Exception) -> bool:
    """识别余额不足（DeepSeek 返回 402 Insufficient Balance）"""
    if getattr(exc, "status_code", None) == 402:
        return True

    msg = str(exc).lower()
    return "insufficient balance" in msg or "402" in msg and "balance" in msg


def polish_with_retry(raw_data: dict, client):
    """
    余额不足时挂起等人充值，回车后重试同一条；
    其他异常原样抛出，交给调用方的既有处理分支。
    """
    while True:
        try:
            return polish_data(raw_data, client)
        except Exception as e:
            if not is_balance_error(e):
                raise

            print(f"\n  💳 API 余额不足：{e}")
            print(f"     请充值后按 Enter 重试本条（Ctrl+C 退出）")

            drain_keys()          # 清掉流式输出期间残留的按键，免得直接被吃掉
            try:
                input("  > ")
            except EOFError:
                # 非交互环境（管道/CI）：没法等人，直接抛出避免死循环
                raise

            print("  ▶️  重试中…\n")


def list_input_files(exclude: set[str] = frozenset()) -> list[str]:
    """扫描 FILES_TO_SQL + EXTRA_INPUT_DIRS，返回待处理文件（排除本次已处理过的）"""
    supported = (".html", ".json", ".txt", ".md")
    scan_dirs = (FILES_TO_SQL_DIR,) + tuple(EXTRA_INPUT_DIRS)
    found: list[str] = []
    for d in scan_dirs:
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if os.path.splitext(f)[1].lower() in supported:
                found.append(os.path.join(d, f))
    return sorted(p for p in found if p not in exclude)


def wait_for_input_files(exclude: set[str], stats: tuple[int, int, int]):
    """目录空了就挂起，直到有新文件投进来"""
    done, failed_db, failed_parse = stats
    watched = "、".join((FILES_TO_SQL_DIR,) + tuple(EXTRA_INPUT_DIRS))
    print(f"\n{'─' * 56}")
    print(f"  📭 {watched} 没有待处理文件"
          f"（本次已入库 {done} 条，入库失败 {failed_db}，解析失败 {failed_parse}）")
    if exclude:
        print(f"  📌 其中 {len(exclude)} 个文件处理失败被保留，本次运行不再重试")
    print(f"  ⏸  等待新文件投放…（Ctrl+C 退出）")

    last_beat = time.monotonic()
    while not list_input_files(exclude):
        time.sleep(FILE_POLL_TICK)

        if time.monotonic() - last_beat >= WAIT_HEARTBEAT:
            last_beat = time.monotonic()
            print(f"     ⏳ 仍在等待新文件…（{time.strftime('%H:%M:%S')}）")

    print(f"  ▶️  检测到新文件，继续解析\n")


def dump_failure(stem: str, raw: str, reason: str) -> str:
    """把解析失败的 AI 原始输出存进 failure/，供事后人工修复"""
    os.makedirs(FAILURE_DIR, exist_ok=True)

    path = os.path.join(FAILURE_DIR, f"{stem}.json")
    dup = 1
    while os.path.exists(path):
        dup += 1
        path = os.path.join(FAILURE_DIR, f"{stem}_{dup}.json")

    # 原始输出原样保留（本身就是非法 JSON），失败原因写进同名 .txt
    with open(path, "w", encoding="utf-8") as f:
        f.write(raw)
    with open(path[:-5] + ".reason.txt", "w", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n{reason}\n")

    return path


def persist_record(sql: str, stem: str) -> bool:
    """
    单条落库：先把 SQL 追加进本次运行的 .sql 存档，再立刻入库。
    返回是否入库成功；失败不抛，让调用方继续跑下一条。
    """
    with open(session_sql_path, "a", encoding="utf-8") as f:
        f.write(f"-- {stem}\n{sql}\n")

    if not ENABLE_DB_IMPORT:
        return True

    try:
        execute_sql(sql, config)
        print(f"  🗄️  已入库")
        return True
    except Exception as e:
        # 这一条已回滚，但 SQL 留在存档文件里，可以事后补执行
        print(f"  ❌ 入库失败（已回滚）：{e}")
        print(f"     SQL 保留在 {session_sql_path}")
        return False


def run():
    os.makedirs(DATA_DIR, exist_ok=True)

    if not os.path.isdir(FILES_TO_SQL_DIR):
        os.makedirs(FILES_TO_SQL_DIR)
        print(f"📂 已创建 {FILES_TO_SQL_DIR}/ 目录")

    watched = (FILES_TO_SQL_DIR,) + tuple(EXTRA_INPUT_DIRS)
    print(f"👀 监听目录：{'、'.join(watched)}"
          f"（EXTRA_INPUT_DIRS 仅扫描，源文件不会删除）")

    provider = _resolve_provider()
    client = make_client(config, provider)
    print(f"🔌 provider = {provider} ({PROVIDERS[provider]['label']})")
    global FORCE_BYPASS_PAUSE
    FORCE_BYPASS_PAUSE = _parse_force_flag()
    if FORCE_BYPASS_PAUSE:
        print("⏩ force-bypass = True，无视禁跑时段")

    global session_sql_path
    session_sql_path = os.path.join(
        DATA_DIR, f"output_{time.strftime('%Y%m%d_%H%M%S')}.sql"
    )

    done = 0                           # 成功入库条数
    failed_db = 0                      # 入库失败条数
    failed_parse = 0                   # 解析失败（已存 failure/）条数
    seen_titles: dict[str, str] = {}   # 润色后 title → 产物 stem，用于查重
    handled: set[str] = set()          # 本次运行处理过但没被删掉的文件，不再重复跑

    while True:
        pending = list_input_files(handled)

        if not pending:
            stats = (done, failed_db, failed_parse)
            wait_for_input_files(handled, stats)
            continue

        file_path = pending[0]
        file_stem = os.path.splitext(os.path.basename(file_path))[0]
        print(f"\n{'─' * 56}")
        print(f"  处理文件：{os.path.basename(file_path)}"
              f"（待处理 {len(pending)} 个）")

        # ── 1. 解析文件（数组 → 多份档案）──────────────────────
        try:
            records = parse_file_to_dicts(file_path)
        except Exception as e:
            print(f"  ❌ 解析失败：{e}，跳过")
            handled.add(file_path)     # 记下来，别在常驻循环里反复重试
            continue

        if len(records) > 1:
            print(f"  📑 JSON 数组，共 {len(records)} 条记录")

        file_ok = 0        # 本文件内成功入库的条数，全中才删源文件

        for rec_idx, raw_data in enumerate(records, 1):
            # 每条开跑前检查禁跑时段：等价于「处理完一条就判断是否该挂起」，
            # 但顺带保证在禁跑时段启动时不会先偷跑一条
            wait_out_pause_window()

            # 数组内的每条记录用 <文件名>_<序号> 作为产物文件名前缀
            stem = file_stem if len(records) == 1 else f"{file_stem}_{rec_idx:02d}"

            if len(records) > 1:
                print(f"\n  ── [{rec_idx}/{len(records)}] {raw_data.get('title', stem)}")

            raw_path = os.path.join(DATA_DIR, f"{stem}.json")
            polished_path = os.path.join(DATA_DIR, f"{stem}_polished.json")

            with open(raw_path, "w", encoding="utf-8") as f:
                json.dump(raw_data, f, ensure_ascii=False, indent=2)

            # ── 2. AI 润色 ─────────────────────────────────────
            print(f"  🤖 AI 处理中（流式输出）...")
            try:
                polished_data = polish_with_retry(raw_data, client)
            except AIRejected as e:
                print(f"  🚫 AI 判定不适合建档：{e}，跳过")
                continue
            except ParseFailed as e:
                path = dump_failure(stem, e.raw, str(e))
                failed_parse += 1
                print(f"  📄 原始输出已存 → {path}，继续下一条")
                continue
            except RuntimeError as e:
                print(f"  ⏭️  {e}，跳过")
                continue
            except Exception as e:
                print(f"  ❌ polish 失败：{e}，跳过")
                continue

            # 配图要在落盘和拼 SQL 之前插入，否则 json 与入库内容都会漏图
            attach_cover_image(polished_data)

            with open(polished_path, "w", encoding="utf-8") as f:
                json.dump(polished_data, f, ensure_ascii=False, indent=2)

            # 落盘成功即算完成，立刻记账（中途崩溃也不会丢这条记录）
            record_done(polished_data, stem)

            # title 是 ON CONFLICT 的冲突键，同批重名会互相覆盖
            new_title = polished_data.get("title")
            if new_title in seen_titles:
                print(f"  ⚠️  标题与 {seen_titles[new_title]} 重复：「{new_title}」"
                      f"，入库时后者会覆盖前者")
            else:
                seen_titles[new_title] = stem

            # ── 3. 生成 SQL 并立即入库（一条一事务）────────────
            try:
                sql = generate_sql(polished_data)
            except Exception as e:
                print(f"  ❌ 生成 SQL 失败：{e}，跳过")
                continue

            if persist_record(sql, stem):
                done += 1
                file_ok += 1
            else:
                failed_db += 1

        # ── 5. 本文件全部入库成功，删掉源文件 ──────────────────
        # 只删 FILES_TO_SQL/ 里的源文件；EXTRA_INPUT_DIRS 是只读资料区，绝不删
        writable_root = os.path.abspath(FILES_TO_SQL_DIR) + os.sep
        in_writable_dir = os.path.abspath(file_path).startswith(writable_root)
        deleted = False
        if (
            DELETE_AFTER_IMPORT and ENABLE_DB_IMPORT
            and file_ok == len(records) and in_writable_dir
        ):
            try:
                os.remove(file_path)
                deleted = True
                print(f"  🗑️  已入库，删除源文件：{os.path.basename(file_path)}")
            except OSError as e:
                print(f"  ⚠️  删除源文件失败：{e}")
        elif DELETE_AFTER_IMPORT and ENABLE_DB_IMPORT:
            if not in_writable_dir:
                print(f"  📚 源文件位于只读目录，保留：{file_path}")
            else:
                print(f"  📌 保留源文件（{file_ok}/{len(records)} 条入库成功）")

        # 没被删掉的文件登记进 handled，避免常驻循环反复处理同一个文件
        if not deleted:
            handled.add(file_path)


def _resolve_provider() -> str:
    """CLI 优先 → 交互式选择 → 默认。返回 provider 名。"""
    if "--provider" in sys.argv:
        i = sys.argv.index("--provider")
        if i + 1 >= len(sys.argv):
            print("⚠️  --provider 需要一个值（如 deepseek / gptsapi）")
            sys.exit(2)
        name = sys.argv[i + 1]
        if name not in PROVIDERS:
            print(f"⚠️  未知 provider：{name}，可选：{', '.join(PROVIDERS)}")
            sys.exit(2)
        return name
    return select_provider_interactive(config, DEFAULT_PROVIDER)


def _parse_force_flag() -> bool:
    """--force / --no-force：强制无视禁跑时段；启动时打印警告。"""
    if "--force" in sys.argv:
        print("⚠️  --force 已启用：无视禁跑时段强制执行")
        return True
    return False


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\n\n⛔ 已手动中止")
