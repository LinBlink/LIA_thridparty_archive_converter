"""
把 tb_archive 里 ref_links 指向 B 站**搜索页**的记录，换成真正的**视频播放页**，
并在 content 首行插入 `@[video](链接)`。

用法（必须在 repo 根目录跑，.env 相对 cwd）：

    python archive_content_markdown_update/fix_bilibili_refs.py --dry-run --limit 30
    python archive_content_markdown_update/fix_bilibili_refs.py            # 正式跑
    python archive_content_markdown_update/fix_bilibili_refs.py --id 1861  # 只跑一条

约定：
- **匹配不确定就不动**。bilibili_search.MATCH_THRESHOLD 以下的一律跳过并记进
  `data/bilibili_unmatched.tsv`，留人工处理——往档案里塞错视频比留个搜索链接更糟。
- **已经有 @[video] 首行的 content 不重复插入**；ref_links 已是 /video/ 链接的直接跳过。
- 一条记录一个事务，UPDATE 前先把 SQL 追加进 `data/bilibili_fix_<时间戳>.sql` 存档
  （沿用 run.py「先存档再入库」的约定，入库失败还能手工 psql 补执行）。
- `data/bilibili_fix_done.txt` 记已处理的 id，重跑自动跳过——这是断点续跑的依据。
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

import psycopg2
from dotenv import dotenv_values

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bilibili_search import Catalog, find_video, is_search_url, video_embed  # noqa: E402
from import_archive import escape_sql_string  # noqa: E402

DATA_DIR = "data"
DONE_FILE = os.path.join(DATA_DIR, "bilibili_fix_done.txt")
UNMATCHED_FILE = os.path.join(DATA_DIR, "bilibili_unmatched.tsv")

# 匹配全在本地目录缓存里做，不打 B 站接口，所以不需要限速
REQUEST_INTERVAL = 0.0


def load_done() -> set:
    if not os.path.exists(DONE_FILE):
        return set()
    with open(DONE_FILE, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def mark_done(archive_id) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DONE_FILE, "a", encoding="utf-8") as f:
        f.write(f"{archive_id}\n")


def log_unmatched(archive_id, title, url) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    new = not os.path.exists(UNMATCHED_FILE)
    with open(UNMATCHED_FILE, "a", encoding="utf-8") as f:
        if new:
            f.write("id\ttitle\tsearch_url\n")
        f.write(f"{archive_id}\t{title}\t{url}\n")


VIDEO_LINE_RE = re.compile(r"^[ \t]*@\[video\]\([^)]*\)[ \t]*$", re.MULTILINE)


def insert_video_line(content: str, url: str) -> str:
    """
    在 markdown 正文最前面插入 @[video](...)，空一行再接原文。

    先把正文里**任何位置**已有的 @[video] 行全部摘掉再插——早期人工加的那条可能在
    文末，只判断首行的话会插成两条。重跑因此是幂等的。
    """
    content = content or ""
    stripped = content.lstrip()
    if stripped.startswith("@[video]("):
        # 首行已经是了：只清掉后面重复的，首行保持不动
        head, sep, rest = stripped.partition("\n")
        rest = VIDEO_LINE_RE.sub("", rest).strip("\n")
        return f"{head}\n\n{rest}" if rest else head
    stripped = VIDEO_LINE_RE.sub("", stripped).strip("\n")
    return f"{video_embed(url)}\n\n{stripped}"


def build_update_sql(archive_id, ref_links, content) -> str:
    return (
        "UPDATE tb_archive SET "
        f"ref_links = {escape_sql_string(json.dumps(ref_links, ensure_ascii=False))}::jsonb, "
        f"content = {escape_sql_string(content)}, "
        "updated_at = NOW() "
        f"WHERE id = {int(archive_id)};\n"
    )


def connect(cfg):
    return psycopg2.connect(
        host=cfg["DB_HOST"], port=cfg["DB_PORT"], dbname=cfg["DB_NAME"],
        user=cfg["DB_USER"], password=cfg["DB_PASSWORD"],
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只搜索和打印，不写库")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少条")
    ap.add_argument("--id", type=int, default=None, help="只处理指定 id")
    ap.add_argument("--random", action="store_true", help="随机取样（配合 --limit 验证命中率）")
    ap.add_argument("--fill-missing", action="store_true",
                    help="只补 content 首行：ref_links 已经是播放页、但正文没有 @[video] 的记录")
    args = ap.parse_args()

    cfg = dotenv_values(".env")
    if not cfg.get("DB_HOST"):
        print("❌ 没读到 .env，请在 repo 根目录运行")
        sys.exit(1)

    catalog = Catalog()
    if not catalog.ups:
        print(f"❌ 没有 UP 投稿目录缓存（{Catalog().path}），先用 bilibili_search.py --build 抓")
        sys.exit(1)
    print(f"📚 目录缓存：{sum(len(v) for v in catalog.data.values())} 个投稿 / {len(catalog.ups)} 个 UP")

    os.makedirs(DATA_DIR, exist_ok=True)
    sql_archive = os.path.join(
        DATA_DIR, f"bilibili_fix_{datetime.now():%Y%m%d_%H%M%S}.sql")

    if args.fill_missing:
        # ref_links 早就换成播放页了（比如人工改过），只是正文还缺 @[video] 首行
        where = ("ref_links::text LIKE '%bilibili.com/video/%' "
                 "AND content NOT LIKE '@[video](%'")
    else:
        where = "ref_links::text ILIKE '%search.bilibili%'"
    if args.id:
        where += f" AND id = {args.id}"
    order = "random()" if args.random else "id"
    sql = f"SELECT id, title, ref_links, content FROM tb_archive WHERE {where} ORDER BY {order}"
    if args.limit:
        sql += f" LIMIT {args.limit}"

    conn = connect(cfg)
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    print(f"📋 待处理 {len(rows)} 条" + ("（dry-run）" if args.dry_run else ""))

    done = set() if args.id else load_done()
    ok = skipped = failed = unmatched = 0

    for i, (aid, title, ref_links, content) in enumerate(rows, 1):
        if str(aid) in done:
            skipped += 1
            continue

        links = ref_links if isinstance(ref_links, list) else json.loads(ref_links or "[]")
        new_links = [dict(l) if isinstance(l, dict) else l for l in links]

        if args.fill_missing:
            # 播放链接已经在 ref_links 里，不用搜，直接拿来补正文首行
            video_url = next((l["url"] for l in links
                              if isinstance(l, dict) and "bilibili.com/video/" in l.get("url", "")), None)
            if not video_url:
                skipped += 1
                continue
            print(f"\n[{i}/{len(rows)}] #{aid} {title[:40]}")
            print(f"   ✅ 补首行 {video_url}")
        else:
            idx = next((j for j, l in enumerate(links)
                        if isinstance(l, dict) and is_search_url(l.get("url", ""))), None)
            if idx is None:
                skipped += 1
                continue

            search_url = links[idx]["url"]
            print(f"\n[{i}/{len(rows)}] #{aid} {title[:40]}")

            try:
                hit = find_video(search_url, catalog)
            except Exception as e:
                print(f"   ❌ 搜索出错：{e}")
                failed += 1
                time.sleep(REQUEST_INTERVAL)
                continue

            if not hit:
                print("   🚫 没有足够相似的视频，跳过（已记入 bilibili_unmatched.tsv）")
                log_unmatched(aid, title, search_url)
                unmatched += 1
                time.sleep(REQUEST_INTERVAL)
                continue

            print(f"   ✅ {hit['score']} {hit['url']}")
            print(f"      {hit['title'][:50]} — {hit.get('up') or '?'}")
            video_url = hit["url"]
            new_links[idx]["url"] = video_url

        new_content = insert_video_line(content, video_url)

        if args.dry_run:
            ok += 1
            time.sleep(REQUEST_INTERVAL)
            continue

        stmt = build_update_sql(aid, new_links, new_content)
        with open(sql_archive, "a", encoding="utf-8") as f:   # 先存档
            f.write(stmt)
        try:                                                   # 再入库
            with conn:
                with conn.cursor() as cur:
                    cur.execute(stmt)
            mark_done(aid)
            ok += 1
        except Exception as e:
            print(f"   ❌ 入库失败：{e}")
            failed += 1

        time.sleep(REQUEST_INTERVAL)

    conn.close()
    print(f"\n── 汇总 ─────────────────\n"
          f"✅ 成功 {ok}   🚫 未匹配 {unmatched}   ⏭️ 跳过 {skipped}   ❌ 失败 {failed}")
    if not args.dry_run and ok:
        print(f"📄 SQL 存档：{sql_archive}")


if __name__ == "__main__":
    main()
