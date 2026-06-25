import sys
import json
import psycopg2
import psycopg2.extras
from dotenv import dotenv_values


def export_archive_data(record_id: int, config: dict) -> dict:
    """核心逻辑：从数据库读取记录，返回 dict"""
    conn = psycopg2.connect(
        host=config["DB_HOST"],
        port=config["DB_PORT"],
        dbname=config["DB_NAME"],
        user=config["DB_USER"],
        password=config["DB_PASSWORD"],
    )
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        "SELECT * FROM tb_archive WHERE id = %s AND deleted_at IS NULL",
        (record_id,)
    )
    row = cur.fetchone()
    if not row:
        raise ValueError(f"找不到 id={record_id}")

    data = dict(row)

    # geometry 字段转 WKT 文本
    if data.get("location"):
        cur.execute(
            "SELECT ST_AsText(location) AS wkt FROM tb_archive WHERE id = %s",
            (record_id,)
        )
        wkt_row = cur.fetchone()
        data["location"] = wkt_row["wkt"] if wkt_row else None

    # timestamptz → ISO 字符串
    for field in ["occurred_at", "closed_at", "created_at", "updated_at", "deleted_at"]:
        if data.get(field):
            data[field] = data[field].isoformat()

    cur.close()
    conn.close()
    return data


# ── 命令行独立使用 ─────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python export.py <id>")
        print("示例: python export.py 1")
        sys.exit(1)

    cfg = dotenv_values(".env")
    record_id = int(sys.argv[1])

    try:
        result = export_archive_data(record_id, cfg)
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)

    import os
    os.makedirs("data", exist_ok=True)
    case_id = result.get("case_id", str(record_id))
    output_path = f"data/archive_{case_id}.json"

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"✅ 已导出到 {output_path}")
