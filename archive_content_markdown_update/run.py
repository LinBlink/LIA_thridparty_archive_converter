import json
import os
import subprocess
import sys
import psycopg2
from dotenv import dotenv_values
from openai import OpenAI

from archive_content_markdown_update.export import export_archive_data
from archive_content_markdown_update.polish import polish_data
from archive_content_markdown_update.import_archive import generate_sql_file

config = dotenv_values(".env")
DATA_DIR = "data"

# 编辑器：优先用环境变量 EDITOR，否则用 VS Code
# 改成 "vim" / "nano" / "notepad" 等均可
EDITOR = os.environ.get("EDITOR", "code")


def get_all_ids() -> list:
    """从数据库取所有未删除记录的 id，按升序"""
    conn = psycopg2.connect(
        host=config["DB_HOST"],
        port=config["DB_PORT"],
        dbname=config["DB_NAME"],
        user=config["DB_USER"],
        password=config["DB_PASSWORD"],
    )
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM tb_archive WHERE deleted_at IS NULL ORDER BY id ASC"
    )
    ids = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()
    return ids


def preview(data: dict):
    """终端展示关键字段摘要"""
    print("\n" + "═" * 56)
    print(f"  📁 case_id      : {data.get('case_id', '-')}")
    print(f"  📌 title        : {data.get('title', '-')}")
    print(f"  🌐 lang         : {'中文 (0)' if data.get('lang') == 0 else '英文 (1)'}")
    print(f"  📍 location     : {data.get('location', '-')}")
    print(f"  📍 location_desc: {data.get('location_desc', '-')}")

    content: str = data.get("content") or ""
    summary = content[:150].replace("\n", " ")
    print(f"  📄 content 摘要 : {summary}{'...' if len(content) > 150 else ''}")

    for field in ["characters", "timelines", "evidence", "ref_links"]:
        val = data.get(field)
        if val:
            if isinstance(val, list):
                count = len(val)
            elif isinstance(val, dict):
                count = len(val.get("nodes", val.get("edges", [])))
            else:
                count = "?"
            print(f"  🔗 {field:<13}: ✅ 已生成（{count} 条）")
        else:
            print(f"  🔗 {field:<13}: ⬜ 空")

    print("═" * 56)


def prompt_user() -> str:
    """交互提示，返回用户选择"""
    while True:
        print("\n  [y] 确认导入   [e] 编辑后重新预览   [s] 跳过   [q] 退出")
        choice = input("  你的选择: ").strip().lower()
        if choice in ("y", "e", "s", "q"):
            return choice
        print("  ⚠️  请输入 y / e / s / q")


def run():
    os.makedirs(DATA_DIR, exist_ok=True)

    ids = get_all_ids()
    total = len(ids)
    if total == 0:
        print("📭 数据库中没有未删除的档案")
        return

    print(f"\n🗂  共找到 {total} 条档案，从 id 最小值开始处理...\n")

    client = OpenAI(
        api_key=config["DEEPSEEK_API_KEY"],
        base_url="https://api.deepseek.com",
    )

    for idx, record_id in enumerate(ids, 1):
        print(f"\n{'─' * 56}")
        print(f"  [{idx}/{total}]  处理 id = {record_id}")

        # ── 1. Export ──────────────────────────────────────────
        try:
            raw_data = export_archive_data(record_id, config)
        except Exception as e:
            print(f"  ❌ export 失败：{e}，跳过")
            continue

        case_id = raw_data.get("case_id", str(record_id))
        raw_path = os.path.join(DATA_DIR, f"archive_{case_id}.json")
        polished_path = os.path.join(DATA_DIR, f"archive_{case_id}_polished.json")

        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(raw_data, f, ensure_ascii=False, indent=2)

        # ── 2. Polish ──────────────────────────────────────────
        print(f"  🤖 AI 处理中（流式输出）...")
        try:
            polished_data = polish_data(raw_data, client)
        except RuntimeError as e:
            print(f"  ⏭️  {e}，跳过 id={record_id}")
            continue
        except Exception as e:
            print(f"  ❌ polish 失败：{e}，跳过")
            continue

        with open(polished_path, "w", encoding="utf-8") as f:
            json.dump(polished_data, f, ensure_ascii=False, indent=2)

        # ── 3. 预览 + 交互 ─────────────────────────────────────
        while True:
            # 每次循环重新读文件（编辑后内容会更新）
            with open(polished_path, "r", encoding="utf-8") as f:
                polished_data = json.load(f)

            preview(polished_data)
            choice = prompt_user()

            if choice == "y":
                sql_path = polished_path.replace("_polished.json", ".sql").replace(".json", ".sql")
                try:
                    generate_sql_file(polished_data, sql_path)
                    print(f"  ✅ SQL 已生成：{sql_path}")
                    print(f"  💡 执行方式：psql -d <dbname> -f {os.path.basename(sql_path)}")
                except Exception as e:
                    print(f"  ❌ 生成 SQL 失败：{e}")
                break

            elif choice == "e":
                print(f"  📝 用编辑器打开：{polished_path}")
                try:
                    subprocess.run([EDITOR, polished_path])
                except FileNotFoundError:
                    print(f"  ⚠️  找不到编辑器 '{EDITOR}'，请手动编辑文件后按 Enter")
                input("  编辑完成后按 Enter 继续预览...")
                # 回到 while True 顶部重新读文件并预览

            elif choice == "s":
                print(f"  ⏭️  跳过 id={record_id}")
                break

            elif choice == "q":
                print("\n  👋 已退出，未处理的档案下次运行时会继续")
                sys.exit(0)

    print(f"\n🎉 全部 {total} 条档案处理完毕")


if __name__ == "__main__":
    run()