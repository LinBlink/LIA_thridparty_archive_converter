"""
Convert true crime JSON data to PostgreSQL INSERT statements for tb_archive table.

JSON field → DB field mapping:
  title          → title
  content (list) → content (joined as plain text)
  org_url        → ref_links (JSON array)
  main_character_name → used in title fallback / characters JSON
  source         → ref_links source description
  author         → author_id placeholder (use 1 as default system user)
  created_at     → created_at / occurred_at (best-effort parse)
  img_urls_captions / yt_video_urls → ref_links

Fields left as NULL/default (require manual input or future enrichment):
  characters, timelines, evidence, location, location_desc, closed_at, tags
"""

import json
import re
import sys
import os
from datetime import datetime, timezone
from typing import Any


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def escape_sql_string(value: str) -> str:
    """Escape single quotes for PostgreSQL string literals."""
    return value.replace("'", "''")


def join_content(content_list: list) -> str:
    """
    The source JSON `content` field is a list of strings where each element
    is already one line/paragraph. Join them with newlines, preserving the
    original structure as-is.
    """
    lines = [item.strip() for item in content_list if item.strip()]
    return '\n'.join(lines)


def build_ref_links(record: dict) -> list[dict]:
    """Assemble a ref_links JSON array from org_url, source, images, videos."""
    links = []

    if record.get('org_url'):
        links.append({
            "type": "article",
            "url": record['org_url'],
            "label": record.get('source', 'Source'),
            "description": f"Original article from {record.get('source', 'unknown source')}"
        })

    for img in record.get('img_urls_captions', []):
        if img.get('url'):
            links.append({
                "type": "image",
                "url": img['url'],
                "label": "Case image",
                "description": img.get('caption', '')
            })

    for vid_url in record.get('yt_video_urls', []):
        if vid_url:
            links.append({
                "type": "video",
                "url": vid_url,
                "label": "Related video",
                "description": ""
            })

    return links


def parse_created_at(date_str: str | None) -> str:
    """
    Parse the source `created_at` string (YYYY-MM-DD) into a TIMESTAMPTZ literal.
    Falls back to NOW() if parsing fails.
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
    Best-effort: look for the earliest explicit date mentioned in the content.
    If nothing found, return NULL.

    Strategy: scan content strings for patterns like "December 9, 1986" or
    "February 1, 2026", pick the earliest one.
    """
    date_patterns = [
        r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+(\d{4})\b',
        r'\b(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\b',
    ]

    month_map = {
        'January': 1, 'February': 2, 'March': 3, 'April': 4,
        'May': 5, 'June': 6, 'July': 7, 'August': 8,
        'September': 9, 'October': 10, 'November': 11, 'December': 12
    }

    found_dates = []
    full_text = ' '.join(str(c) for c in record.get('content', []))

    for item in full_text.split('\n'):
        for pat in date_patterns:
            for match in re.finditer(pat, item):
                groups = match.groups()
                try:
                    if groups[0] in month_map:
                        # "Month DD, YYYY"
                        month = month_map[groups[0]]
                        day = int(match.group(0).split()[1].rstrip(','))
                        year = int(groups[1])
                    else:
                        # "DD Month YYYY"
                        day = int(groups[0])
                        month = month_map[groups[1]]
                        year = int(groups[2])
                    found_dates.append(datetime(year, month, day, tzinfo=timezone.utc))
                except (ValueError, IndexError):
                    pass

    if found_dates:
        earliest = min(found_dates)
        return f"'{earliest.isoformat()}'"
    return "NULL"


def build_insert(record: dict, author_id: int = 1) -> str:
    """Generate a single PostgreSQL INSERT statement for one JSON record."""

    title = escape_sql_string(record.get('title') or record.get('main_character_name') or '无标题')
    content_text = escape_sql_string(join_content(record.get('content', [])))
    ref_links = build_ref_links(record)
    ref_links_json = escape_sql_string(json.dumps(ref_links, ensure_ascii=False))
    created_at_sql = parse_created_at(record.get('created_at'))
    occurred_at_sql = infer_occurred_at(record)

    # Source-based type heuristic:
    #   thetruecrimedatabase.com → third-party (2)
    #   default → civilian archive (0)
    source = record.get('source', '')
    if 'truecrime' in source or 'database' in source:
        archive_type = 2   # 第三方档案
    else:
        archive_type = 0   # 民间档案

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
    NULL,
    NULL,
    NULL,
    {archive_type},
    '{ref_links_json}'::json,
    0,
    {occurred_at_sql},
    NULL,
    {author_id},
    NULL,
    NULL,
    0,
    {created_at_sql},
    {created_at_sql},
    NULL,
    0
);"""
    return sql


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def convert_file(input_path: str, output_path: str, author_id: int = 1) -> None:
    """Read a JSON file (list of records) and write SQL INSERT statements."""

    with open(input_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if isinstance(data, dict):
        # Single record
        records = [data]
    elif isinstance(data, list):
        records = data
    else:
        raise ValueError("JSON root must be an object or an array of objects.")

    statements = []
    for i, record in enumerate(records):
        try:
            sql = build_insert(record, author_id=author_id)
            statements.append(sql)
        except Exception as e:
            print(f"[WARN] Skipped record #{i} due to error: {e}", file=sys.stderr)

    header = (
        "-- Auto-generated by convert_to_sql.py\n"
        "-- Review NULL fields (characters / timelines / evidence / location) before executing.\n"
        f"-- Generated at: {datetime.now(timezone.utc).isoformat()}\n"
        f"-- Total records: {len(statements)}\n\n"
        "BEGIN;\n\n"
    )
    footer = "\n\nCOMMIT;\n"

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(header)
        f.write('\n\n'.join(statements))
        f.write(footer)

    print(f"✅ Done — {len(statements)} INSERT(s) written to: {output_path}")


# ──────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description='Convert true-crime JSON export to PostgreSQL INSERT statements for tb_archive.'
    )
    parser.add_argument('input',  help='Path to input JSON file')
    parser.add_argument('output', nargs='?', default=None,
                        help='Path to output .sql file (default: <input>.sql)')
    parser.add_argument('--author-id', type=int, default=1,
                        help='author_id to assign to all records (default: 1)')

    args = parser.parse_args()

    out = args.output or (os.path.splitext(args.input)[0] + '.sql')
    convert_file(args.input, out, author_id=args.author_id)