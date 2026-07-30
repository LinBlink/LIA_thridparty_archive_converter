import json
import os
import sys
import time
from dotenv import dotenv_values
from html.parser import HTMLParser
from openai import OpenAI

from polish import polish_data, AIRejected
from import_archive import generate_sql

config = dotenv_values(".env")
DATA_DIR = "data"
FILES_TO_SQL_DIR = "FILES_TO_SQL"
BATCH_SIZE = 10          # 每攒够多少条 SQL 就落一次盘
MIN_CONTENT_LEN = 200    # 正文短于此字数的记录直接舍弃，不送 AI
JOBS_DONE_FILE = "jobs_done.txt"   # 已完成的 content_id 流水，每行一条


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


def normalize_item(item: dict, fallback_title: str) -> dict:
    """把单条 JSON 记录归一化成含 title / content 的档案 dict"""
    if not isinstance(item, dict):
        raise ValueError(f"JSON 记录不是对象：{type(item).__name__}")

    # 知乎抓取格式：正文在 content_text
    if "content" not in item and "content_text" in item:
        item = zhihu_to_archive(item)

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




def record_done(data: dict, stem: str):
    """AI 处理完一条就往 jobs_done.txt 追加一行，供后续追溯/去重"""
    marker = data.get("content_id") or stem
    with open(JOBS_DONE_FILE, "a", encoding="utf-8") as f:
        f.write(f"{marker}\n")


def flush_sql(sql_blocks: list[str]) -> str | None:
    """把已攒下的 SQL 落成一个以时间戳命名的文件，返回路径"""
    if not sql_blocks:
        return None

    ts = time.strftime("%Y%m%d_%H%M%S")
    output_sql = os.path.join(DATA_DIR, f"output_{ts}.sql")

    # 同一秒内多次落盘时补后缀，避免互相覆盖
    dup = 1
    while os.path.exists(output_sql):
        dup += 1
        output_sql = os.path.join(DATA_DIR, f"output_{ts}_{dup}.sql")

    with open(output_sql, "w", encoding="utf-8") as f:
        f.write("\n\n".join(sql_blocks) + "\n")

    print(f"\n  💾 已落盘 {len(sql_blocks)} 条 → {output_sql}\n")
    return output_sql


def run():
    os.makedirs(DATA_DIR, exist_ok=True)

    if not os.path.isdir(FILES_TO_SQL_DIR):
        os.makedirs(FILES_TO_SQL_DIR)
        print(f"📂 已创建 {FILES_TO_SQL_DIR}/ 目录，请将文件放入后重新运行")
        return

    supported = (".html", ".json", ".txt", ".md")
    all_files = sorted(
        os.path.join(FILES_TO_SQL_DIR, f)
        for f in os.listdir(FILES_TO_SQL_DIR)
        if os.path.splitext(f)[1].lower() in supported
    )

    if not all_files:
        print(f"📭 {FILES_TO_SQL_DIR}/ 中没有 .html / .json / .txt 文件")
        return

    total = len(all_files)
    print(f"\n🗂  共找到 {total} 个文件，开始处理...\n")

    client = OpenAI(
        api_key=config["DEEPSEEK_API_KEY"],
        base_url="https://api.deepseek.com",
    )

    sql_blocks: list[str] = []         # 当前这批还没落盘的 SQL
    written: list[str] = []            # 已落盘的 .sql 路径
    done = 0                           # 累计成功条数
    seen_titles: dict[str, str] = {}   # 润色后 title → 产物 stem，用于查重

    for idx, file_path in enumerate(all_files, 1):
        file_stem = os.path.splitext(os.path.basename(file_path))[0]
        print(f"\n{'─' * 56}")
        print(f"  [{idx}/{total}]  处理文件：{os.path.basename(file_path)}")

        # ── 1. 解析文件（数组 → 多份档案）──────────────────────
        try:
            records = parse_file_to_dicts(file_path)
        except Exception as e:
            print(f"  ❌ 解析失败：{e}，跳过")
            continue

        if len(records) > 1:
            print(f"  📑 JSON 数组，共 {len(records)} 条记录")

        for rec_idx, raw_data in enumerate(records, 1):
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
                polished_data = polish_data(raw_data, client)
            except AIRejected as e:
                print(f"  🚫 AI 判定不适合建档：{e}，跳过")
                continue
            except RuntimeError as e:
                print(f"  ⏭️  {e}，跳过")
                continue
            except Exception as e:
                print(f"  ❌ polish 失败：{e}，跳过")
                continue

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

            # ── 3. 收集 SQL，每满 BATCH_SIZE 条落一次盘 ─────────
            try:
                sql = generate_sql(polished_data)
                sql_blocks.append(f"-- {stem}\n{sql}")
                done += 1
                print(f"  ✅ SQL 已收集（{len(sql_blocks)}/{BATCH_SIZE}）")
            except Exception as e:
                print(f"  ❌ 生成 SQL 失败：{e}，跳过")
                continue

            if len(sql_blocks) >= BATCH_SIZE:
                written.append(flush_sql(sql_blocks))
                sql_blocks = []

    # ── 4. 收尾：不足一批的余量也落盘 ──────────────────────────
    tail = flush_sql(sql_blocks)
    if tail:
        written.append(tail)

    if written:
        print(f"\n✅ 共 {done} 条记录，分 {len(written)} 个文件：")
        for p in written:
            print(f"   • {p}")
        print(f"💡 执行方式：psql -d <dbname> -f <上面任一文件>")
    else:
        print("\n⚠️  没有成功生成任何 SQL")

    print(f"\n🎉 全部 {total} 个文件处理完毕")


if __name__ == "__main__":
    run()
