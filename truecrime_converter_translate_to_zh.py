"""
将真实犯罪案件 JSON 数据转换为 tb_archive 表的 PostgreSQL INSERT 语句。
title 和 content 字段会先通过本地 Ollama 翻译为中文，再写入 SQL。

JSON 字段 → 数据库字段 对应关系：
  title               → title（档案标题，翻译后）
  content (列表)      → content（逐行翻译后按行拼接）
  org_url             → ref_links（参考链接 JSON 数组）
  main_character_name → title 的备用值（title 为空时使用）
  source              → ref_links 中的来源描述
  author              → 暂不使用，author_id 由命令行参数指定
  created_at          → created_at / updated_at
  img_urls_captions   → ref_links（图片类型）
  yt_video_urls       → ref_links（视频类型）

以下字段暂置 NULL，需后续人工补充或 AI 提取：
  characters（人物关系）、timelines（时间线）、evidence（证据链）
  location（坐标）、location_desc（地点描述）、closed_at（结案时间）
"""

import json
import re
import sys
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone


# ──────────────────────────────────────────────
# Ollama 翻译
# ──────────────────────────────────────────────

OLLAMA_BASE_URL = "http://192.168.2.11:11434"
OLLAMA_MODEL    = "qwen3:8b"

# 系统提示：让模型只返回翻译结果，不加任何解释或思考标签
TRANSLATE_SYSTEM_PROMPT = (
    "你是一名专业翻译。将用户提供的英文文本翻译为简体中文。"
    "要求：\n"
    "1. 只输出翻译结果，不加任何解释、前缀或后缀\n"
    "2. 保持原文的段落和换行结构\n"
    "3. 专有名词（人名、地名）音译或保留原文均可\n"
    "4. 不要输出 <think> 等思考标签中的内容"
)


def ollama_translate(text: str) -> str:
    """
    调用本地 Ollama API，将英文文本翻译为中文。

    使用 /api/chat 接口（非流式），传入 system prompt + user message。
    若调用失败则抛出异常，由上层决定是跳过还是中断。

    参数：
      text - 待翻译的英文字符串
    返回：
      翻译后的中文字符串
    """
    url = f"{OLLAMA_BASE_URL}/api/chat"

    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "stream": False,          # 非流式，等待完整响应
        "options": {
            "temperature": 0.1,   # 低温，减少随机性，让翻译更稳定
        },
        "messages": [
            {"role": "system", "content": TRANSLATE_SYSTEM_PROMPT},
            {"role": "user",   "content": text},
        ]
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            # 返回结构：{"message": {"role": "assistant", "content": "..."}}
            raw = body["message"]["content"].strip()
            # 过滤掉 qwen3 thinking 模式可能输出的 <think>...</think> 块
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
            return raw
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ollama 连接失败（{OLLAMA_BASE_URL}）：{e}") from e


def translate_title(title: str) -> str:
    """
    翻译单条档案标题。
    直接把 title 字符串发给 Ollama，返回翻译结果。
    """
    print(f"  [翻译标题] {title[:60]}{'...' if len(title) > 60 else ''}")
    result = ollama_translate(title)
    print(f"  [标题结果] {result[:60]}{'...' if len(result) > 60 else ''}")
    return result


def translate_content_lines(content_list: list) -> list[str]:
    """
    逐行翻译 content 列表。

    策略：每个非空列表元素单独发一次翻译请求，保留原始分行结构。
    空行原样保留（作为空字符串），不消耗 API 调用。

    参数：
      content_list - 原始 content 字段（字符串列表）
    返回：
      翻译后的字符串列表，长度与原列表一致
    """
    translated = []
    total = len(content_list)

    for idx, line in enumerate(content_list):
        stripped = line.strip()
        if not stripped:
            # 空行直接保留，不翻译
            translated.append("")
            continue

        print(f"  [翻译正文 {idx+1}/{total}] {stripped[:50]}{'...' if len(stripped) > 50 else ''}")
        result = ollama_translate(stripped)
        translated.append(result)

    return translated


# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────

def escape_sql_string(value: str) -> str:
    """将字符串中的单引号转义为两个单引号，防止 SQL 注入 / 语法错误。"""
    return value.replace("'", "''")


def join_content(content_list: list) -> str:
    """
    将 content 列表拼接为纯文本。
    原始 JSON 中每个列表元素对应正文的一行，直接用换行符连接，过滤空行。
    """
    lines = [item.strip() for item in content_list if item.strip()]
    return '\n'.join(lines)


def build_ref_links(record: dict) -> list[dict]:
    """
    从 JSON 记录中提取所有参考链接，组装成 ref_links 数组。
    包含三类：
      - article：原文链接（org_url）
      - image：图片（img_urls_captions）
      - video：视频（yt_video_urls）
    """
    links = []

    # 原文链接
    if record.get('org_url'):
        links.append({
            "type": "article",
            "url": record['org_url'],
            "label": record.get('source', 'Source'),
            "description": f"原始文章来源：{record.get('source', '未知来源')}"
        })

    # 图片链接
    for img in record.get('img_urls_captions', []):
        if img.get('url'):
            links.append({
                "type": "image",
                "url": img['url'],
                "label": "案件图片",
                "description": img.get('caption', '')
            })

    # 视频链接
    for vid_url in record.get('yt_video_urls', []):
        if vid_url:
            links.append({
                "type": "video",
                "url": vid_url,
                "label": "相关视频",
                "description": ""
            })

    return links


def parse_created_at(date_str: str | None) -> str:
    """
    将 JSON 中的 created_at 字符串（格式 YYYY-MM-DD）转换为 SQL TIMESTAMPTZ 字面量。
    解析失败时返回 NOW()，让数据库自动填入当前时间。
    """
    if not date_str:
        return "NOW()"
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return f"'{dt.isoformat()}'"
    except ValueError:
        return "NOW()"


def infer_occurred_at(record: dict) -> str:
    """
    从原始（翻译前）的正文内容中推断事件发生时间（occurred_at）。

    策略：用正则扫描 content 列表中的英文日期格式，例如：
      - "December 9, 1986"（月 日, 年）
      - "9 December 1986"（日 月 年）
    取所有匹配到的日期中最早的那个作为事件发生时间。
    找不到任何日期则返回 NULL。

    注意：必须在翻译之前调用，否则日期格式已变为中文，正则无法匹配。
    """
    date_patterns = [
        # 格式一："Month DD, YYYY" 例如 December 9, 1986
        r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+(\d{4})\b',
        # 格式二："DD Month YYYY" 例如 9 December 1986
        r'\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\b',
    ]

    # 英文月份名 → 数字
    month_map = {
        'January': 1, 'February': 2, 'March': 3, 'April': 4,
        'May': 5, 'June': 6, 'July': 7, 'August': 8,
        'September': 9, 'October': 10, 'November': 11, 'December': 12
    }

    found_dates = []
    full_text = ' '.join(str(c) for c in record.get('content', []))

    for pat in date_patterns:
        for match in re.finditer(pat, full_text):
            groups = match.groups()
            try:
                if groups[0] in month_map:
                    # 格式一：第一个捕获组是月份名
                    month = month_map[groups[0]]
                    day = int(match.group(0).split()[1].rstrip(','))
                    year = int(groups[1])
                else:
                    # 格式二：第一个捕获组是日，第二个是月份名，第三个是年
                    day = int(groups[0])
                    month = month_map[groups[1]]
                    year = int(groups[2])
                found_dates.append(datetime(year, month, day, tzinfo=timezone.utc))
            except (ValueError, IndexError):
                # 日期数值非法（如 day=32）则跳过
                pass

    if found_dates:
        earliest = min(found_dates)
        return f"'{earliest.isoformat()}'"
    return "NULL"


def build_insert(record: dict, author_id: int = 1) -> str:
    """
    将单条 JSON 记录转换为一条完整的 INSERT INTO tb_archive SQL 语句。
    调用前 record 中的 title 和 content 应已替换为翻译后的中文版本。

    参数：
      record    - 单条 JSON 记录（dict），title/content 已翻译
      author_id - 写入 author_id 字段的用户 ID，默认为 1（系统默认用户）
    """

    # title：优先取 title 字段，为空则用 main_character_name，再空则用"无标题"
    title = escape_sql_string(
        record.get('title') or record.get('main_character_name') or '无标题'
    )

    # content：列表按行拼接后转义
    content_text = escape_sql_string(join_content(record.get('content', [])))

    # ref_links：组装后序列化为 JSON 字符串再转义
    ref_links = build_ref_links(record)
    ref_links_json = escape_sql_string(json.dumps(ref_links, ensure_ascii=False))

    # 时间字段
    created_at_sql = parse_created_at(record.get('created_at'))
    occurred_at_sql = record.get('_occurred_at_sql', 'NULL')  # 由主流程预先计算并存入

    # type 字段推断：
    #   来源域名含 truecrime / database → 2（第三方档案）
    #   其余 → 0（民间档案）
    source = record.get('source', '')
    if 'truecrime' in source or 'database' in source:
        archive_type = 2
    else:
        archive_type = 0

    # 拼装 INSERT 语句
    # 注意：ref_links 字段使用 ::json 强制转型，告诉 PostgreSQL 这是 JSON 值
    sql = f"""INSERT INTO tb_archive (
    title,
    content,
    characters,
    timelines,
    evidence,
    type,
    ref_links,
    status,
    occurred_at,
    closed_at,
    author_id,
    location,
    location_desc,
    view_count,
    created_at,
    updated_at,
    deleted_at,
    is_private
) VALUES (
    '{title}',
    '{content_text}',
    NULL,           -- characters：人物关系图，待后续录入
    NULL,           -- timelines：时间线图，待后续录入
    NULL,           -- evidence：证据链，待后续录入
    {archive_type},
    '{ref_links_json}'::json,
    0,              -- status：0=更新中
    {occurred_at_sql},
    NULL,           -- closed_at：结案时间未知
    {author_id},
    NULL,           -- location：地理坐标，待地理编码后补充
    NULL,           -- location_desc：地点描述，待补充
    0,
    {created_at_sql},
    {created_at_sql},
    NULL,           -- deleted_at：NULL 表示未删除
    0               -- is_private：0=公开
);"""
    return sql


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────

def convert_file(input_path: str, output_path: str, author_id: int = 1) -> None:
    """
    读取 JSON 文件，对每条记录的 title 和 content 调用 Ollama 翻译后，
    生成 INSERT 语句并写入 .sql 输出文件。

    处理顺序（每条记录）：
      1. 用原始英文内容推断 occurred_at（正则只认英文日期）
      2. 翻译 title
      3. 逐行翻译 content 列表
      4. 用翻译后的内容生成 INSERT 语句

    支持两种 JSON 结构：
      - 顶层为数组：[ {...}, {...} ]  → 批量处理
      - 顶层为对象：{ ... }          → 单条处理
    """
    with open(input_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if isinstance(data, dict):
        records = [data]          # 单条记录包装成列表统一处理
    elif isinstance(data, list):
        records = data
    else:
        raise ValueError("JSON 根节点必须是对象或数组。")

    statements = []
    total = len(records)

    for i, record in enumerate(records):
        print(f"\n{'='*50}")
        print(f"[{i+1}/{total}] 处理记录：{record.get('title', '(无标题)')}")
        print(f"{'='*50}")

        try:
            # ① 翻译之前先提取日期（正则只能识别英文月份名）
            occurred_at_sql = infer_occurred_at(record)

            # ② 翻译 title
            original_title = record.get('title') or record.get('main_character_name') or ''
            translated_title = translate_title(original_title) if original_title else ''

            # ③ 逐行翻译 content
            translated_content = translate_content_lines(record.get('content', []))

            # ④ 将翻译结果写回 record 副本（不修改原始数据）
            translated_record = {
                **record,
                'title':           translated_title,
                'content':         translated_content,
                '_occurred_at_sql': occurred_at_sql,  # 预计算的时间值，供 build_insert 使用
            }

            sql = build_insert(translated_record, author_id=author_id)
            statements.append(sql)
            print(f"  ✅ 记录 #{i+1} 生成完成")

        except Exception as e:
            # 单条记录出错时打印警告并跳过，不中断整批处理
            print(f"  ❌ 记录 #{i+1} 跳过，原因：{e}", file=sys.stderr)

    # 输出文件头部注释 + 事务包裹
    header = (
        "-- 由 convert_to_sql.py 自动生成（含 Ollama 中文翻译）\n"
        "-- 执行前请检查 NULL 字段：characters / timelines / evidence / location\n"
        f"-- 生成时间：{datetime.now(timezone.utc).isoformat()}\n"
        f"-- 模型：{OLLAMA_MODEL}  服务：{OLLAMA_BASE_URL}\n"
        f"-- 共 {len(statements)} 条记录\n\n"
        "BEGIN;\n\n"
    )
    footer = "\n\nCOMMIT;\n"

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(header)
        f.write('\n\n'.join(statements))
        f.write(footer)

    print(f"\n✅ 全部完成 — 已生成 {len(statements)} 条 INSERT，输出至：{output_path}")


# ──────────────────────────────────────────────
# 命令行入口
# ──────────────────────────────────────────────

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description='将真实犯罪 JSON 数据翻译为中文并转换为 tb_archive 的 PostgreSQL INSERT 语句'
    )
    parser.add_argument('input',  help='输入 JSON 文件路径')
    parser.add_argument('output', nargs='?', default=None,
                        help='输出 .sql 文件路径（默认与输入文件同名，扩展名改为 .sql）')
    parser.add_argument('--author-id', type=int, default=1,
                        help='写入所有记录的 author_id（默认：1）')
    parser.add_argument('--ollama-url', default=OLLAMA_BASE_URL,
                        help=f'Ollama 服务地址（默认：{OLLAMA_BASE_URL}）')
    parser.add_argument('--model', default=OLLAMA_MODEL,
                        help=f'使用的 Ollama 模型（默认：{OLLAMA_MODEL}）')

    args = parser.parse_args()

    # 支持通过命令行覆盖模块级常量
    OLLAMA_BASE_URL = args.ollama_url
    OLLAMA_MODEL    = args.model

    out = args.output or (os.path.splitext(args.input)[0] + '.sql')
    convert_file(args.input, out, author_id=args.author_id)