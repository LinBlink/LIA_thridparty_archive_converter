import sys
import json
import tempfile
import os
import subprocess
from openai import OpenAI
from dotenv import dotenv_values

# ── 不让 AI 处理的字段 ────────────────────────────────────────
SKIP_FIELDS = {
    "id", "case_id", "type", "status", "view_count",
    "author_id", "created_at", "updated_at", "deleted_at",
    "is_private", "occurred_at", "closed_at",
    "_originals",
}

EDITOR = os.environ.get("EDITOR", "code")

SYSTEM_PROMPT = """你是一名专业的异常现象档案整理员，负责对档案进行全面整理与结构化处理。

你将收到一份 JSON 格式的档案，请按照以下规则处理并返回完整的 JSON。

## 处理规则

### title（档案标题）
- 润色标题，使其简洁、神秘、严肃
- 语言与 content 主体语言保持一致

### lang（语言标记）
- 根据 content 主体语言判断：中文 → 0，英文 → 1

### content（档案正文）
- 润色正文：修正错别字、语病，优化段落结构
- 保持神秘、严肃的异常事件档案文风
- 不得添加、删减或捏造任何事实
- 如果原有的 ref-links 中有图片或视频链接，可以在文章中间合适处插入

### location_desc（地点文字描述）
- 根据 content 内容推断事件发生的详细位置
- 格式如："广东省广州市天河区某小区" 或 "Unknown"（无法判断时）

### location（PostGIS 坐标 WGS84）
- 根据 location_desc 推断大概经纬度
- 格式为字符串 "POINT(经度 纬度)"，如 "POINT(113.2644 23.1291)"
- 无法判断时填 null

### characters（人物关系图）
nodes 字段：id, name, role, tags, description
edges 字段：source, target, base_relation, interactions（含 action/timestamp/detail）
示例格式：
{
  "nodes": [
    {"id": "u1", "name": "张三", "role": "受害者", "tags": ["大学生"], "description": "..."},
    {"id": "u2", "name": "李四", "role": "目击者", "tags": ["路人"], "description": "..."}
  ],
  "edges": [
    {
      "source": "u2", "target": "u1",
      "base_relation": "陌生人",
      "interactions": [
        {"action": "目击", "timestamp": "2023-10-26T23:15:00Z", "detail": "..."}
      ]
    }
  ]
}
- 从 content 中提取所有人物构建图结构
- 如原有数据，在其基础上补充完善
- 无人物时填 null

### timelines（事件时间线）
每条含：id, time_type(precise/fuzzy/uncertain), timestamp(ISO8601或null), time_display, title, content, importance(critical/high/normal), related_characters, tags
示例格式：
[
  {
    "id": "t1",
    "time_type": "precise",
    "timestamp": "2023-10-25T14:30:00Z",
    "time_display": "2023年10月25日 14:30",
    "title": "事件标题",
    "content": "事件描述",
    "importance": "normal",
    "related_characters": ["u1", "u2"],
    "tags": ["前因", "线索"]
  }
]
- 从 content 中提取关键事件节点，按时间排序
- 如原有数据，在其基础上补充完善
- 无时间线时填 null

### evidence（证据链）
nodes 字段：id, name, type(physical/testimonial/biological/digital/documentary), reliability(critical/high/medium/low), description, source, related_characters, related_timelines
edges 字段：source, target, relation_type(leads_to/corroborates/contradicts/derived_from), description
示例格式：
{
  "nodes": [
    {
      "id": "e1", "name": "证据名称",
      "type": "physical", "reliability": "high",
      "description": "...", "source": "...",
      "related_characters": ["u1"], "related_timelines": ["t1"]
    }
  ],
  "edges": [
    {"source": "e1", "target": "e2", "relation_type": "leads_to", "description": "..."}
  ]
}
- 从 content 中提取证据，构建图结构
- 如原有数据，在其基础上补充完善
- 无证据时填 null

### ref_links（参考链接）
- 仅保留新闻、论文、百科等正式来源链接
- 格式：[{"title": "链接标题", "url": "https://..."}]
- 不放图片/视频等媒体文件链接
- 无引用时填 null

## 输出要求
- 只返回纯 JSON，不要任何解释、markdown 代码块、前言或后记
- 保留原 JSON 中所有字段，只修改上述涉及的字段
- JSON 必须合法可解析
"""

# =========================
# 1. AI STREAM（已加固）
# =========================
def stream_ai(client: OpenAI, payload: dict) -> str:
    print("\n  ┌─ AI 输出 " + "─" * 44)

    chunks = []
    full_text = ""

    stream = client.chat.completions.create(
        model="deepseek-v4-flash",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
        ],
        temperature=0.3,
        max_tokens=384000,
        stream=True,
    )

    finish_reason = None

    for chunk in stream:
        if not getattr(chunk, "choices", None):
            continue

        choice = chunk.choices[0]

        # ✅ 防 crash：delta/content 安全访问
        delta = None
        if getattr(choice, "delta", None):
            delta = getattr(choice.delta, "content", None)

        if delta:
            print(delta, end="", flush=True)
            chunks.append(delta)

        if getattr(choice, "finish_reason", None):
            finish_reason = choice.finish_reason

    full_text = "".join(chunks)

    print("\n  └" + "─" * 53)

    if finish_reason == "length":
        print("\n  ⚠️ 警告：输出被截断（max_tokens）JSON 可能不完整")

    return full_text


# =========================
# 2. JSON 清理（增强）
# =========================
def clean_json_str(raw: str) -> str:
    cleaned = raw.strip()

    # 去 markdown code block
    if "```" in cleaned:
        parts = cleaned.split("```")
        if len(parts) >= 3:
            cleaned = parts[1]
        cleaned = cleaned.strip()

    # 防止 AI 输出前后杂字符
    first = cleaned.find("{")
    last = cleaned.rfind("}")
    if first != -1 and last != -1:
        cleaned = cleaned[first:last + 1]

    return cleaned.strip()


# =========================
# 3. 编辑器（防卡死）
# =========================
def open_editor_for_correction(raw_text: str) -> str:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", encoding="utf-8", delete=False
    ) as tmp:
        tmp.write(raw_text)
        tmp_path = tmp.name

    print(f"\n  📝 打开编辑器：{tmp_path}")

    try:
        subprocess.run([EDITOR, tmp_path], check=False)
    except Exception as e:
        print(f"  ⚠️ 编辑器启动失败：{e}")

    # 防 CI 卡死：可跳过 input
    try:
        input("  修改完成后按 Enter 继续...")
    except EOFError:
        pass

    try:
        with open(tmp_path, "r", encoding="utf-8") as f:
            corrected = f.read()
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    return corrected


# =========================
# 4. JSON 解析 + 人工修复流
# =========================
def parse_with_correction(raw: str) -> dict:
    current = raw
    attempts = 0

    # ---------- 阶段1：JSON合法性 ----------
    while True:
        attempts += 1
        if attempts > 10:
            raise RuntimeError("解析失败次数过多，终止")

        cleaned = clean_json_str(current)

        try:
            parsed = json.loads(cleaned)
            break
        except json.JSONDecodeError as e:
            print(f"\n  ❌ JSON解析失败: {e}")
            print("  [e] 手动修复  [r] 重新生成  [x] 放弃")
            choice = input("  选择: ").strip().lower()

            if choice == "e":
                current = open_editor_for_correction(cleaned)
            elif choice == "r":
                raise RuntimeError("REGENERATE")
            else:
                raise RuntimeError("ABORT")

    # ---------- 阶段2：人工确认 ----------
    while True:
        print("\n  ✅ JSON解析成功")
        print("  [y] 确认  [e] 编辑  [r] 重生成")

        choice = input("  选择: ").strip().lower()

        if choice == "y":
            return parsed

        elif choice == "e":
            edited = open_editor_for_correction(
                json.dumps(parsed, ensure_ascii=False, indent=2)
            )
            try:
                parsed = json.loads(clean_json_str(edited))
            except json.JSONDecodeError as e:
                print(f"  ❌ 修改后仍非法: {e}")
                current = edited

        elif choice == "r":
            raise RuntimeError("REGENERATE")

        else:
            print("  ⚠️ 输入 y / e / r")


# =========================
# 5. 主逻辑
# =========================
def polish_data(data: dict, client: OpenAI) -> dict:
    originals = {k: v for k, v in data.items() if k not in SKIP_FIELDS and v}
    data["_originals"] = originals

    payload = {k: v for k, v in data.items() if k not in SKIP_FIELDS}

    while True:
        raw = stream_ai(client, payload)

        try:
            ai_result = parse_with_correction(raw)
        except RuntimeError as e:
            msg = str(e)

            if "REGENERATE" in msg:
                print("\n  🔄 重新调用 AI ...")
                continue
            elif "ABORT" in msg:
                raise
            else:
                raise

        break

    # 合并结果（安全覆盖）
    for k, v in ai_result.items():
        data[k] = v

    return data


# =========================
# CLI
# =========================
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python polish.py <input.json>")
        sys.exit(1)

    cfg = dotenv_values(".env")
    client = OpenAI(
        api_key=cfg["DEEPSEEK_API_KEY"],
        base_url="https://api.deepseek.com"
    )

    input_path = sys.argv[1]

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    print("🤖 AI处理中...")

    try:
        result = polish_data(data, client)
    except RuntimeError as e:
        print(f"⏭️ 终止: {e}")
        sys.exit(0)

    output_path = input_path.replace(".json", "_polished.json")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 完成: {output_path}")