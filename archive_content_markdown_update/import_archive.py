import sys
import json
import os
from dotenv import dotenv_values


# 不写入数据库的字段（tb_archive 无对应列，混进去会让 INSERT 报未知列）
SKIP_FIELDS = {
    "id", "created_at", "updated_at", "deleted_at",
    "_originals", "_original_content",
    "desc",              # 只用于喂 AI 做主题前置判断
    "content_id",        # 来源侧原始 ID，只用于 jobs_done.txt 追溯
    "skip", "reason",    # AI 的「不适合建档」标记
}

# 需要序列化为 JSON 字符串的字段
JSON_FIELDS = {"characters", "timelines", "evidence", "ref_links"}

# geometry 字段
GEO_FIELD = "location"


def escape_sql_string(value) -> str:
    """
    将 Python 值转为 SQL 字面量字符串，安全处理单引号和特殊类型。
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    # 字符串：单引号转义为两个单引号（标准 SQL 转义）
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def generate_sql(data: dict) -> str:
    """
    核心逻辑：接收 dict，生成 INSERT ... ON CONFLICT (title) DO UPDATE SQL 字符串。
    不连接数据库，纯字符串拼接。
    """
    title = data.get("title")
    if not title:
        raise ValueError("JSON 中没有 title SQL")

    # 过滤字段：去掉跳过字段和 geometry
    fields = {
        k: v for k, v in data.items()
        if k not in SKIP_FIELDS and k != GEO_FIELD
    }

    # JSON 字段序列化为字符串
    for jf in JSON_FIELDS:
        if jf in fields and isinstance(fields[jf], (dict, list)):
            fields[jf] = json.dumps(fields[jf], ensure_ascii=False)

    geo_value = data.get(GEO_FIELD)

    # ── 构建列名和值 ───────────────────────────────────────────
    col_names = list(fields.keys())
    col_values = [escape_sql_string(v) for v in fields.values()]

    # geometry 单独处理，用 ST_GeomFromText
    if geo_value:
        col_names.append(GEO_FIELD)
        col_values.append(f"ST_GeomFromText({escape_sql_string(geo_value)}, 4326)")

    # ── INSERT 部分 ────────────────────────────────────────────
    cols_sql = ", ".join(col_names)
    vals_sql = ", ".join(col_values)

    # ── ON CONFLICT DO UPDATE 部分（排除 title 本身）──────────
    update_parts = []
    for col, val in zip(col_names, col_values):
        if col == "title":
            continue  # 冲突键本身不更新
        update_parts.append(f"    {col} = EXCLUDED.{col}")
    update_parts.append("    updated_at = NOW()")

    update_sql = ",\n".join(update_parts)

    sql = (
        f"INSERT INTO tb_archive ({cols_sql})\n"
        f"VALUES ({vals_sql})\n"
        f"ON CONFLICT (title) DO UPDATE SET\n"
        f"{update_sql};\n"
    )

    return sql


def generate_sql_file(data: dict, output_path: str):
    """生成 .sql 文件，供调用方使用"""
    sql = generate_sql(data)
    title = data.get("title", "unknown")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(f"-- 档案 upsert：title = {title}\n")
        f.write(f"-- 生成时间：NOW()\n")
        f.write(f"-- 执行方式：psql -d <dbname> -f {os.path.basename(output_path)}\n\n")
        f.write(sql)

    return sql


# ── 命令行独立使用 ─────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python import_archive.py <polished.json>")
        print("示例: python import_archive.py data/archive_CASE-2024-001_polished.json")
        sys.exit(1)

    input_path = sys.argv[1]

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    output_path = input_path.replace("_polished.json", ".sql").replace(".json", ".sql")

    try:
        sql = generate_sql_file(data, output_path)
        print(f"✅ 已生成 SQL 文件：{output_path}")
        print("\n── 预览 ──────────────────────────────────────────")
        # 只预览前 20 行
        lines = sql.splitlines()
        for line in lines[:20]:
            print(line)
        if len(lines) > 20:
            print(f"  ... 共 {len(lines)} 行")
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)