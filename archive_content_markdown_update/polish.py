import sys
import json
import re
import tempfile
import os
import subprocess
from openai import OpenAI
from dotenv import dotenv_values

import select             # POSIX 回退用（Windows 下不会走到）

try:
    import msvcrt          # Windows：无阻塞读键
except ImportError:
    msvcrt = None

# ── 不让 AI 处理的字段 ────────────────────────────────────────
SKIP_FIELDS = {
    "id", "case_id", "type", "view_count",
    "author_id", "created_at", "updated_at", "deleted_at",
    "is_private", "closed_at",
    "_originals",
    "content_id",      # 来源侧的原始 ID，只用于 jobs_done.txt 追溯
}

EDITOR = os.environ.get("EDITOR", "code")

# ── 多 API provider 支持 ───────────────────────────────────────
# 旧链路走 DeepSeek 官方；新链路走 gptsapi.net。
# run.py / translate_en.py 启动时可选其一，CLI 默认旧链路。
PROVIDERS = {
    "deepseek": {
        "api_key_env": "DEEPSEEK_API_KEY",
        "base_url_env": "DEEPSEEK_BASE_URL",
        "base_url_default": "https://api.deepseek.com",
        "label": "DeepSeek 官方（旧）",
    },
    "gptsapi": {
        "api_key_env": "GPTSAPI_API_KEY",
        "base_url_env": "GPTSAPI_BASE_URL",
        "base_url_default": "https://api.gptsapi.net",
        "label": "gptsapi.net（新）",
    },
}
DEFAULT_PROVIDER = "deepseek"


def make_client(cfg, provider: str = DEFAULT_PROVIDER) -> OpenAI:
    """根据 provider 名称从 cfg 里挑出对应的 key + base_url 构造 client。
    未识别名字视为默认；缺失对应环境变量立即抛错，避免静默用错 key。"""
    spec = PROVIDERS.get(provider, PROVIDERS[DEFAULT_PROVIDER])
    api_key = cfg.get(spec["api_key_env"])
    base_url = cfg.get(spec["base_url_env"]) or spec["base_url_default"]
    if not api_key:
        raise RuntimeError(
            f"provider={provider} 缺少 {spec['api_key_env']}，请在 .env 补上"
        )
    return OpenAI(api_key=api_key, base_url=base_url)


def select_provider_interactive(cfg, default: str = DEFAULT_PROVIDER) -> str:
    """启动时交互式选择 provider，回车即用 default；非交互环境走 default。"""
    if not sys.stdin.isatty():
        return default

    print("─" * 56)
    print("  选择 API provider：")
    for i, key in enumerate(PROVIDERS, 1):
        marker = "（默认）" if key == default else ""
        print(f"    {i}. {PROVIDERS[key]['label']} {marker}")
    print("─" * 56)

    while True:
        try:
            ans = input(f"  输入编号或名称 [{default}]: ").strip()
        except EOFError:
            return default
        if not ans:
            return default
        if ans.isdigit():
            keys = list(PROVIDERS.keys())
            idx = int(ans) - 1
            if 0 <= idx < len(keys):
                return keys[idx]
        if ans in PROVIDERS:
            return ans
        print(f"  ⚠️  无效输入：{ans!r}")

SYSTEM_PROMPT = """你是一名专业的异常现象档案整理员，负责对档案进行全面整理与结构化处理。

你将收到一份 txt 格式的视频字幕，请按照以下规则处理并返回完整的 JSON。

## 处理规则

### title（档案标题）
- 润色，吸引眼球但是也要规范，格式符合一个档案的标题

### lang（语言标记）
- 根据 content 主体语言判断：中文 → 0，英文 → 1

### content（档案正文）
- 禁止提及视频的作者（比如L探员、邓肯等），这个档案要当做自己整理的一样
- 禁止照抄、照搬甚至原样输出给你的文本
- 所有文字均以第三人称客观叙述
- 润色正文：修正错别字、语病，优化段落结构
- 对于成分比较复杂的事件，多交代当时的历史现实社会背景，便于读者理解当时社会环境
- 优化文章markdown结构，可以增加一级二级三级等标题，注意不要添加主标题。引子标题不要写“引子”二字
- 对于时间、人物、地点、值得重视的文本等，做加粗标注
- 要写的富有故事性，引人入胜，字数至少5000字。字数至少5000字。字数至少5000字。
- 不得捏造事实，不得捏造事实，不得捏造事实！
- 这是JSON中的markdown字符串，注意格式！注意格式！注意格式！
- 如果原有的材料中有图片或视频链接，必须在文章中合适处插入，禁止插在结尾。如果是来自知乎的视频链接，选择不插入。
- 如果原有的 ref-links 中有图片或视频链接，必须在文章中合适处插入，禁止插在结尾。

### location_desc（地点文字描述）
- 根据 content 内容推断事件发生的详细位置
- 格式如："广东省广州市天河区某小区"
- 无地点时填 null

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
- 一定要和异常或案件相关

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
- 一定要和异常或案件相关

### ref_links（参考链接）
- 格式：[{"title": "链接标题", "url": "https://..."}]
- 在此次输出中，title为：B站链接-[此处严格写上给你的文本首行，但是去除类似“—【2020-10-30】-中文”字样]
- url为：https://search.bilibili.com/all?keyword=[此处严格写上给你的文本首行，但是去除类似“—【2020-10-30】-中文”字样]

### status（结案状态）
- 根据 content 内容判断案件/事件是否已有明确结论
- 0 = 未结案（事件未解明、仍在调查、结局不明）
- 1 = 已结案（有官方定论、法院判决、事件已有公认解释）
- 无法判断时填 0

### occurred_at
- 根据正文推断出异常事件发生时间
- 尽量精确到秒
- 该时间要符合 postgreSQL 时间录入格式
- 示例：2026-06-25T05:46:15.201067+00:00

### tags（标签）
- 从以下预置标签中选取 1~5 个最符合内容的标签名，组成字符串数组
- 预置标签列表：
  | name             | 适用场景                             |
  |------------------|--------------------------------------|
  | 丢失失踪         | 人员或物品的异常失踪事件             |
  | 外星人           | 涉及疑似外星生命的报告               |
  | 不明飞行物       | UFO / UAP 目击记录                   |
  | 刑事案件         | 有明确违法行为的案件                 |
  | 道听途说         | 来源为二手或口口相传，可信度存疑     |
  | 真实案件         | 有官方记录或新闻报道佐证             |
  | 证据确凿         | 存在可验证的物证或影像证据           |
  | 电子游戏世界异常 | 游戏内出现的超出设计范围的异象       |
  | 请提高警惕       | 事件存在潜在危险，提醒读者注意       |
  | 荒诞误会         | 经核实为误解或巧合的事件             |
  | 极低概率事件     | 统计意义上罕见但有合理解释的现象     |
  | 灵魂鬼怪         | 涉及灵异、鬼魂或超自然现象的报告     |
- 如果预置的标签不太符合要求，可以自己填入你自定义的标签
- 示例：["丢失失踪", "道听途说", "证据确凿"]
- 无法判断时填 null

## 输出要求
- 只返回纯 JSON，不要任何解释、markdown 代码块、前言或后记
- 保留原 JSON 中所有字段，只修改上述涉及的字段
- JSON 必须合法可解析
"""

# =========================
# 0. 生成中按键跳过
# =========================
SKIP_KEYS = {"s", "S", "\x1b"}      # s 或 Esc


class SkipGeneration(RuntimeError):
    """用户在流式生成过程中按键要求跳过当前这条"""


class AIRejected(RuntimeError):
    """AI 判定该条与灵异/真实案件主题无关，不适合建档"""


class ParseFailed(RuntimeError):
    """JSON 解析失败且自动修复无效；raw 保存原始输出，交由调用方落到 failure/"""

    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw


# 解析失败时是否停下来问人（[e]/[r]/[x]）。
# 默认 False：跑批时直接抛 ParseFailed，由 run.py 存进 failure/ 后继续下一条。
INTERACTIVE_REPAIR = False


def _keyboard_ready() -> bool:
    """只有真正接在终端上才启用按键检测（管道/CI 下直接关掉）"""
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def drain_keys():
    """清空生成开始前残留的按键，避免上一条的回车误触发"""
    if not _keyboard_ready():
        return
    if msvcrt:
        while msvcrt.kbhit():
            msvcrt.getwch()
    else:
        while select.select([sys.stdin], [], [], 0)[0]:
            sys.stdin.readline()


def skip_requested() -> bool:
    """无阻塞探测是否按下了跳过键"""
    if not _keyboard_ready():
        return False

    if msvcrt:
        while msvcrt.kbhit():
            ch = msvcrt.getwch()
            if ch in ("\x00", "\xe0"):   # 功能键：吃掉后半个扫描码
                msvcrt.getwch()
                continue
            if ch in SKIP_KEYS:
                return True
        return False

    # POSIX 回退：未设 cbreak，需要按 s 后回车
    while select.select([sys.stdin], [], [], 0)[0]:
        line = sys.stdin.readline()
        if not line:
            return False
        if line.strip() in SKIP_KEYS or line.strip().lower() == "skip":
            return True
    return False


# =========================
# 1. AI STREAM（已加固）
# =========================
def stream_ai(client: OpenAI, payload: dict) -> str:
    hint = "（按 s 或 Esc 跳过本条）" if _keyboard_ready() else ""
    print("\n  ┌─ AI 输出 " + "─" * 44)
    if hint:
        print(f"  {hint}")

    drain_keys()

    chunks = []
    full_text = ""

    stream = client.chat.completions.create(
        model="deepseek-v4-flash",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
        ],
        temperature=0.7,
        max_tokens=384000,
        stream=True,
    )

    finish_reason = None

    for chunk in stream:
        # 每收到一块就探一次键盘：命中就立刻断流，不等模型写完
        if skip_requested():
            try:
                stream.close()
            except Exception:
                pass
            print("\n  └" + "─" * 53)
            raise SkipGeneration("已按键停止生成")

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
# 3.5 自动修复：AI 常在字符串里吐裸换行
# =========================
def escape_newlines_in_strings(raw: str) -> str:
    """
    把 JSON 字符串字面量内部的裸换行转义成 \\n。
    这是 AI 最常见的破格方式（markdown 正文直接带真换行），
    转义后既能解析成功，又保住 content 的段落与标题结构。
    """
    out = []
    in_string = False
    escaped = False

    for ch in raw:
        if escaped:                      # 上一个字符是反斜杠，本字符原样保留
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue

        if in_string:
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                continue                 # CR 直接丢掉
            if ch == "\t":
                out.append("\\t")
                continue

        out.append(ch)

    return "".join(out)


def strip_newlines(raw: str) -> str:
    """兜底：把所有换行清空（会压平 markdown 结构，仅在转义也救不回来时用）"""
    return re.sub(r"\r?\n", "", raw)


# 按顺序尝试，谁先成功用谁
AUTO_REPAIRS = (
    ("转义字符串内的换行", escape_newlines_in_strings),
    ("清空所有换行", strip_newlines),
)


def try_auto_repair(cleaned: str):
    """依次套用自动修复，返回 (parsed, 修复方式名)；全失败返回 (None, None)"""
    for name, fix in AUTO_REPAIRS:
        try:
            repaired = fix(cleaned)
        except Exception:
            continue

        if repaired == cleaned:          # 没改动就不必重试
            continue

        try:
            return json.loads(repaired), name
        except json.JSONDecodeError:
            continue

    return None, None


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
            # 先自动修复，成功就直接继续，不打扰人
            parsed, how = try_auto_repair(cleaned)
            if parsed is not None:
                print(f"\n  🔧 自动修复成功（{how}）")
                break

            print(f"\n  ❌ JSON解析失败: {e}")
            print("  🔧 自动修复无效（已试：" + "、".join(n for n, _ in AUTO_REPAIRS) + "）")

            # 跑批模式：不阻塞问人，把原始输出交出去存档后继续下一条
            if not INTERACTIVE_REPAIR:
                raise ParseFailed(f"JSON 解析失败: {e}", cleaned)

            print("  [e] 手动修复  [r] 重新生成  [x] 放弃")
            choice = input("  选择: ").strip().lower()

            if choice == "e":
                current = open_editor_for_correction(cleaned)
            elif choice == "r":
                raise RuntimeError("REGENERATE")
            else:
                raise RuntimeError("ABORT")

    print("\n  ✅ JSON解析成功")
    return parsed


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

        # AI 判定该条不适合建档：约定返回 {"skip": true, "reason": "..."}
        if ai_result.get("skip"):
            raise AIRejected(ai_result.get("reason") or "AI 判定不适合建档")

        break

    # 合并结果（安全覆盖）
    for k, v in ai_result.items():
        data[k] = v

    return data


# =========================
# CLI
# =========================
if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("用法: python polish.py <input.json> [--provider deepseek|gptsapi]")
        sys.exit(1 if len(sys.argv) < 2 else 0)

    args = sys.argv[1:]
    provider = DEFAULT_PROVIDER
    if "--provider" in args:
        i = args.index("--provider")
        provider = args[i + 1]
        args = args[:i] + args[i + 2:]

    if not args:
        print("用法: python polish.py <input.json> [--provider deepseek|gptsapi]")
        sys.exit(1)

    cfg = dotenv_values(".env")
    client = make_client(cfg, provider)
    print(f"  🔌 provider = {provider} ({PROVIDERS[provider]['label']})")

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