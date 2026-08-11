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

# ── 解码器兜底 ────────────────────────────────────────────────
# response_format=json_object 让接口在采样阶段就约束住语法：括号、引号、逗号
# 由解码器保证，模型吐不出非法 JSON——这一类错误（漏键名、少半个引号、数组提前
# 闭合）从此不该再出现。它管不了**语义**（字段名对不对、枚举值合不合法），
# 那部分仍由 SYSTEM_PROMPT + schema_check.py 负责。
# 注意：DeepSeek 要求提示词里出现 "JSON" 字样才允许开启，SYSTEM_PROMPT 已满足。
# 接口不认这个参数时 stream_ai 会自动降级并把这个开关置 False，不中断跑批。
ENABLE_JSON_MODE = True
JSON_RESPONSE_FORMAT = {"type": "json_object"}

# ── 多 API provider 支持 ───────────────────────────────────────
# 旧链路走 DeepSeek 官方；新链路走 gptsapi.net。
# run.py / translate_en.py 启动时可选其一，CLI 默认旧链路。
PROVIDERS = {
    "deepseek": {
        "api_key_env": "DEEPSEEK_API_KEY",
        "base_url_env": "DEEPSEEK_BASE_URL",
        "base_url_default": "https://api.deepseek.com",
        "label": "DeepSeek 官方",
        "model": "deepseek-v4-flash",
        "max_tokens": 384000,
        "extra_body": None,                # deepseek-v4-flash 无特殊参数
    },
    "gptsapi": {
        "api_key_env": "GPTSAPI_API_KEY",
        "base_url_env": "GPTSAPI_BASE_URL",
        "base_url_default": "https://api.gptsapi.net",
        "label": "gptsapi.net",
        "model": "deepseek-v4-flash",
        "max_tokens": 384000,
        "extra_body": None,
    },
    "minimax": {
        "api_key_env": "MINIMAX_API_KEY",
        "base_url_env": "MINIMAX_BASE_URL",
        "base_url_default": "https://api.minimaxi.com/v1",
        "label": "MiniMax（MiniMax-M2.7）",
        "model": "MiniMax-M2.7",
        "max_tokens": 196608,
        # M2.x 系列不支持关闭 thinking；reasoning_split=True 把思考内容拆到
        # reasoning_details 字段，content 字段只剩最终答案（不再混 <think> 标签）。
        # 思考仍会发生、要付 token，但输出干净；唯一能做到的折中。
        "extra_body": {"reasoning_split": True},
    },
}
DEFAULT_PROVIDER = "deepseek"


def make_client(cfg, provider: str = DEFAULT_PROVIDER) -> tuple[OpenAI, str, int, dict | None]:
    """根据 provider 名称从 cfg 里挑出对应的 key + base_url 构造 client，
    并返回该 provider 的默认 model、max_tokens 与 extra_body（None 表示不传）。
    未识别名字视为默认；缺失对应环境变量立即抛错，避免静默用错 key。"""
    spec = PROVIDERS.get(provider, PROVIDERS[DEFAULT_PROVIDER])
    api_key = cfg.get(spec["api_key_env"])
    base_url = cfg.get(spec["base_url_env"]) or spec["base_url_default"]
    if not api_key:
        raise RuntimeError(
            f"provider={provider} 缺少 {spec['api_key_env']}，请在 .env 补上"
        )
    return (
        OpenAI(api_key=api_key, base_url=base_url),
        spec["model"],
        spec["max_tokens"],
        spec.get("extra_body"),
    )


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
你的输出是一个 JSON 对象，会被直接解析入库、再由前端渲染给读者看。

你返回的语言必须和给你的文章或字幕语言一致！

## 输出形态

只输出一个 JSON 对象。语法（括号、引号、逗号、转义）由接口的 JSON 模式保证，
你不用为此分心，但下面两件事解码器管不了，要靠你自己：

1. **写得完**。输出预算有限，写到一半被截断整份档案就作废。
   `characters` / `timelines` / `evidence` 每类控制在 15 个节点以内，
   宁可少写几个节点，也不要写到超长被砍断。内容写不出来就填 null。
2. **键名要对**。见下面的字段白名单。

## 第一优先级：字段白名单（多写一个键，后端就读不出这份档案）

`characters` / `evidence` / `ref_links` 会被后端用 Jackson 反序列化成固定的 Java 类，
**这些类不接受未知字段**：你自创一个键，数据能写进库，但用户打开档案时后端会直接报错。
所以这三个字段里的每个对象，**只许出现下面列出的键，一个都不许多**（可以少写、可以填 null）：

- `characters.nodes[]`：`id`、`name`、`role`、`tags`、`description`
- `characters.edges[]`：`source`、`target`、`base_relation`、`interactions`
- `characters.edges[].interactions[]`：`action`、`timestamp`、`detail`
- `evidence.nodes[]`：`id`、`name`、`type`、`reliability`、`description`、`source`、
  `related_characters`、`related_timelines`
- `evidence.edges[]`：`source`、`target`、`relation_type`、`description`、`related_timelines`
- `ref_links[]`：`title`、`url`

✗ `{"id": "u1", "name": "张三", "age": 33, "occupation": "教师"}`（`age`/`occupation` 不存在 → 后端报错）
✓ `{"id": "u1", "name": "张三", "tags": ["33岁", "教师"], "description": "..."}`（想补充信息就写进 tags 或 description）

`timelines[]` 相对宽容，但同样请只用：`id`、`time_type`、`timestamp`、`time_display`、
`title`、`content`、`importance`、`related_characters`、`tags`。

另外，`edges` 里 `source` / `target` 引用的 id **必须真的在同一个字段的 `nodes` 里出现过**，
不许引用不存在的节点。

## 处理规则

### title（档案标题）
- 润色，吸引眼球但是也要规范，格式符合一个档案的标题

### lang（语言标记）
- 你返回的语言必须和给你的文章或字幕语言一致！
- 根据 content 主体语言判断：中文 → 0，英文 → 1
- 必须是**数字**字面量 `0` / `1`，不能写成字符串 `"0"`

### content（档案正文）
- 你返回的语言必须和给你的文章或字幕语言一致！
- 如果你的文本是视频字幕形式禁止提及视频的作者（比如L探员、邓肯等），这个档案要当做自己整理的一样
- 如果你拿到的 json 有 "header_img_url" ， 则变成markdown形式在头部插入。如果有"img_urls_captions"，在文字合适地方插入。如果有 "yt_video_urls" 也在合适地方插入
- 禁止照抄、照搬甚至原样输出给你的文本
- 所有文字均以第三人称客观叙述
- 润色正文：修正错别字、语病，优化段落结构
- 对于成分比较复杂的事件，多交代当时的历史现实社会背景，便于读者理解当时社会环境
- 对于时间、人物、地点、值得重视的文本等，做加粗标注
- **正文会用 marked + DOMPurify 渲染，下面几条直接决定显示效果：**
  - **标题只用 `##`（二级）和 `###`（三级）**。`#` 一级留给档案标题本身、不要在正文里写；
    `####` 及更深的标题不会进右侧目录、也没有锚点，等于白写。引子标题不要写“引子”二字
  - **渲染开了 `breaks: true`，单个换行就是一次强制断行**。所以：段落之间必须空一行（`\\n\\n`），
    而**一个段落内部绝对不许为了排版折行**——句子中间敲换行，页面上就会断成两截
  - 图片写 `![图注](url)`，方括号里的文字会**显示成图片下方的说明文字**。
    所以要写一句有信息量的图注（如「案发现场的监控截图」），不要写档案标题、不要留空
  - 视频写成 `@[video](链接)` **独占一行**，链接里不能有空格。只有 B 站、YouTube、
    以及 .mp4/.webm 等直链视频文件能内嵌播放，其它地址会退化成一条普通链接
  - 可以放心使用：GFM 表格、`>` 引用、`-`/`1.` 列表、`~~删除线~~`、`---` 分隔线、行内代码
  - **禁止写裸 HTML 标签**（`<div>`、`<br>`、`<img>` 等），会被安全过滤直接丢掉
- 要写的富有故事性，引人入胜，字数至少5000字。字数至少5000字。字数至少5000字。
- 不得捏造事实，不得捏造事实，不得捏造事实！
- 这是**写在 JSON 字符串里的 markdown**，格式硬性要求：
  - 段落之间的空行、标题前后的换行，一律写成 `\\n`，不许敲真实回车
  - 正文里要用引号时一律用中文引号「」或“”，**不要用英文双引号 "**，从根上避免转义出错
  - 正文里出现反斜杠（如 Windows 路径）要写成 `\\\\`
- 如果原有的材料中有图片或视频链接，必须在文章中合适处插入，禁止插在结尾。如果是来自知乎的视频链接，选择不插入。
- 如果原有的 ref-links 中有图片或视频链接，必须在文章中合适处插入，禁止插在结尾。

### location_desc（地点文字描述）
- 你返回的语言必须和给你的文章或字幕语言一致！
- 根据 content 内容推断事件发生的详细位置
- 格式如："广东省广州市天河区某小区"
- 无地点时填 null

### location（PostGIS 坐标 WGS84）
- 根据 location_desc 推断大概经纬度
- 格式为字符串 "POINT(经度 纬度)"，如 "POINT(113.2644 23.1291)"
- 无法判断时填 null

### characters（人物关系图）
- 你返回的语言必须和给你的文章或字幕语言一致！
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
- 你返回的语言必须和给你的文章或字幕语言一致！
每条含：id, time_type, timestamp(ISO8601或null), time_display, title, content, importance, related_characters, tags
**枚举值只能从下列取，写别的前端渲染不出来：**
- time_type：`precise` | `fuzzy`
- importance：`critical` | `high` | `normal`
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
- 你返回的语言必须和给你的文章或字幕语言一致！
nodes 字段：id, name, type, reliability, description, source, related_characters, related_timelines
edges 字段：source, target, relation_type, description, related_timelines
**枚举值只能从下列取，写别的前端渲染不出来：**
- type：`physical`（实物）| `documentary`（文件）| `testimonial`（证词）| `video`（影像）| `audio`（音频）
- reliability：`high` | `medium` | `low`（**没有 critical**）
- relation_type：`corroborates` | `leads_to` | `derived_from` | `contradicts` | `supports`
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
    {"source": "e1", "target": "e2", "relation_type": "leads_to",
     "description": "...", "related_timelines": ["t1"]}
  ]
}
- 从 content 中提取证据，构建图结构
- 如原有数据，在其基础上补充完善
- 无证据时填 null
- 一定要和异常或案件相关

### ref_links（参考链接）
- 你返回的语言必须和给你的文章或字幕语言一致！
- 格式：[{"title": "链接标题", "url": "https://..."}]
- 在此次输出中，如果title为：B站链接-[此处严格写上给你的文本首行，但是去除类似“—【2020-10-30】-中文”字样]
- url为：https://search.bilibili.com/all?keyword=[此处严格写上给你的文本首行，但是去除类似“—【2020-10-30】-中文”字样]
- 如果你拿到的json包含org_url，则插入此处



### status（结案状态）
- 根据 content 内容判断案件/事件是否已有明确结论
- 0 = 更新中 / 未结案（事件未解明、仍在调查、结局不明）
- 1 = 已完结 / 已结案（有官方定论、法院判决、事件已有公认解释）
- 必须是**数字**字面量 `0` / `1`，不能写成字符串；无法判断时填 0

### closed_at（结案时间）
- 仅当 status = 1 时填写：官方定论、判决或事件收束的时间
- 格式同 occurred_at（带时区的 ISO8601）
- status = 0 或时间不明时填 null

### occurred_at
- 根据正文推断出异常事件发生时间
- 尽量精确到秒
- 该时间要符合 postgreSQL 时间录入格式
- 示例：2026-06-25T05:46:15.201067+00:00

### tags（标签）
- 你返回的语言必须和给你的文章或字幕语言一致！
- 从以下预置标签中选取 1~5 个最符合内容的标签名，组成字符串数组（中文档案用中文标签，英文档案用英文标签）
- 预置标签列表：
  | name             | 适用场景                             |
  |------------------|--------------------------------------|
  | 丢失失踪 Missing Persons         | 人员或物品的异常失踪事件             |
  | 外星人  Extraterrestrial          | 涉及疑似外星生命的报告               |
  | 不明飞行物  UFO     | UFO / UAP 目击记录                   |
  | 刑事案件 Crime Case        | 有明确违法行为的案件                 |
  | 道听途说 Hearsay        | 来源为二手或口口相传，可信度存疑     |
  | 真实案件 Verified Case         | 有官方记录或新闻报道佐证             |
  | 证据确凿 Conclusive Evidence        | 存在可验证的物证或影像证据           |
  | 电子游戏世界异常 Video Game Anomaly | 游戏内出现的超出设计范围的异象       |
  | 请提高警惕 Stay Alert      | 事件存在潜在危险，提醒读者注意       |
  | 荒诞误会 Absurd Misunderstanding        | 经核实为误解或巧合的事件             |
  | 极低概率事件 Extremely Rare Event    | 统计意义上罕见但有合理解释的现象     |
  | 灵魂鬼怪 Ghosts & Spirits         | 涉及灵异、鬼魂或超自然现象的报告     |
- 如果预置的标签不太符合要求，可以自己填入你自定义的标签
- 示例：["丢失失踪", "道听途说", "证据确凿"]
- 无法判断时填 null

## 输出要求
- 只返回纯 JSON，不要任何解释、markdown 代码块、前言或后记
- 保留原 JSON 中所有字段，只修改上述涉及的字段
- 再次强调开头两条：**每个对象只许出现白名单上的键**（多一个键后端就读不出这份档案）；
  **结构字段每类 ≤15 个节点**，宁可少写也不要被截断。
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
def _is_response_format_error(exc: Exception) -> bool:
    """接口不认 response_format 的报错（各家措辞不一，按关键词认）"""
    msg = str(exc).lower()
    return "response_format" in msg or "response format" in msg


def stream_ai(client: OpenAI, payload: dict, model: str, max_tokens: int, extra_body: dict | None = None) -> str:
    hint = "（按 s 或 Esc 跳过本条）" if _keyboard_ready() else ""
    print("\n  ┌─ AI 输出 " + "─" * 44)
    if hint:
        print(f"  {hint}")

    drain_keys()

    chunks = []
    full_text = ""

    extra = {"extra_body": extra_body} if extra_body else {}
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
    ]

    def _create(json_mode: bool):
        fmt = {"response_format": JSON_RESPONSE_FORMAT} if json_mode else {}
        return client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.7,
            max_tokens=max_tokens,
            stream=True,
            **fmt,
            **extra,
        )

    # 解码器兜底：约束采样，模型在语法层面就吐不出非法 JSON。
    # 个别中转/自建接口不认这个参数，报错就降级重发一次，不因此整条失败。
    global ENABLE_JSON_MODE
    if ENABLE_JSON_MODE:
        try:
            stream = _create(True)
        except Exception as e:
            if not _is_response_format_error(e):
                raise
            print(f"  ⚠️  接口不支持 response_format，本次运行起降级为纯提示词约束：{e}")
            ENABLE_JSON_MODE = False
            stream = _create(False)
    else:
        stream = _create(False)

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
            safe_print(delta)
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
def safe_print(text: str):
    """打印流式增量：模型可能吐出孤立代理字符，直接 print 会 UnicodeEncodeError。
    显示不出来无所谓，但绝不能因为「打印不出来」把跑批打断。"""
    try:
        print(text, end="", flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(enc, "replace").decode(enc, "replace"),
              end="", flush=True)


def strip_surrogates(obj):
    """递归去掉字符串里的**孤立代理字符**（U+D800–U+DFFF）。

    模型偶尔会吐半个 emoji 代理对（JSON 里写成 `\\ud83d` 却没有配对的低位），
    `json.loads` 会**照单全收**变成一个孤立代理码点，之后任何一步碰它都炸：
    `json.dump(..., ensure_ascii=False)` 落盘报 UnicodeEncodeError（run.py 里这步
    没有 try，直接把常驻进程干掉——「跑一段时间自己停了」的元凶之一）、
    print 到终端报同样的错、psycopg2 写库也过不去。
    在解析出口处一次性洗掉，后面所有环节就都不用操心了。
    """
    if isinstance(obj, str):
        if not any("\ud800" <= c <= "\udfff" for c in obj):
            return obj
        return "".join(c for c in obj if not "\ud800" <= c <= "\udfff")
    if isinstance(obj, dict):
        return {k: strip_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [strip_surrogates(v) for v in obj]
    return obj


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
    """本地自动修复 →（可选）人工修复。

    语法层面的兜底交给解码器（provider 的 response_format=json_object），
    模型在采样阶段就吐不出非法 JSON，所以这里不再花第二次调用去让 AI 改 JSON。
    """
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
    # 出口统一洗掉孤立代理字符，别让它流到落盘/入库那一步才炸
    return strip_surrogates(parsed)


# =========================
# 5. 主逻辑
# =========================
def polish_data(data: dict, client: OpenAI, model: str, max_tokens: int, extra_body: dict | None = None) -> dict:

    originals = {k: v for k, v in data.items() if k not in SKIP_FIELDS and v}
    data["_originals"] = originals

    payload = {k: v for k, v in data.items() if k not in SKIP_FIELDS}

    while True:
        raw = stream_ai(client, payload, model, max_tokens, extra_body)

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
    client, model, max_tokens, extra_body = make_client(cfg, provider)
    print(f"  🔌 provider = {provider} ({PROVIDERS[provider]['label']}, model={model}, max_tokens={max_tokens}, extra_body={extra_body})")

    input_path = sys.argv[1]

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    print("🤖 AI处理中...")

    try:
        result = polish_data(data, client, model, max_tokens, extra_body)
    except RuntimeError as e:
        print(f"⏭️ 终止: {e}")
        sys.exit(0)

    output_path = input_path.replace(".json", "_polished.json")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 完成: {output_path}")