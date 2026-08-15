"""failure/ 里的坏档案 → 修复 → 入库。

run.py 里的主流水线不做任何 AI 修复：AI 输出解析不了、或四个 jsonb 字段结构不合规，
原样扔进 `failure/`（原文 `<stem>.json` + 失败原因 `<stem>.reason.txt`）。这个脚本是
那批文件的**离线补救**：修好就补配图、拼 SQL、入库，成功的挪进 `failure/fixed/`。

## 省 token 是第一原则

坏文件动辄一两万字，整篇丢给 AI 重写既贵又容易在重写时改坏正文。所以修复分三档，
**能本地修的绝不调 AI，要调 AI 也只发出问题的那一小段**：

1. **本地无损修**（0 token）：`sanitize_json_text` 带前瞻扫一遍，修四类机械可判的破格
   （裸控制字符、非法转义、字符串里的裸引号、缺失的逗号），再套一遍
   `polish.try_auto_repair`，然后 `normalize_local` 做结构归一 + 剪枝。
   解析得了且结构合规就到此为止，**一个 token 都不花**（实测 157 个历史坏文件里 40 个如此）。
2. **AI 整篇重生成**（`regenerate`，**只调一次，不重试**）：本地搞不定就把整篇坏 JSON
   发给 AI，要它按 `REGEN_PROMPT` 原样吐回一份合法合规的 JSON。同名 `.reason.txt`
   存在就把当初的失败原因一起发过去（解析器报的错最能指出该往哪儿看）。

  没有第三档。重生成失败、或结果仍不合规、或正文明显被缩写（`MIN_CONTENT_RATIO`），
  一律跳过这条、原文留在 `failure/` 等人工处理——**不重试**。
  早先那版「定点修复」（截出错处 ±400 字符、让 AI 回 find/replace 最小编辑、
  最多 6 轮）已删除：命中率只有 19%，且大量失败是模型答案对但字面对不上被校验挡掉，
  协议本身比它要解决的问题还复杂。

## 用法

```powershell
python archive_content_markdown_update/failure_fix.py              # 修完一轮后驻留等新文件
python archive_content_markdown_update/failure_fix.py --once       # 修完一轮就退出
python archive_content_markdown_update/failure_fix.py --dry-run    # 只修不入库，结果写 data/<stem>_fixed.json
python archive_content_markdown_update/failure_fix.py --no-ai --dry-run --once   # 0 token，看本地能修多少
python archive_content_markdown_update/failure_fix.py --dry-run failure/xxx.json # 只调试指定文件
```

其它开关：`--provider deepseek|gptsapi|minimax`、`--model <名字>`（修复是小活，
可以挑比主流水线更快更便宜的模型）、`--limit N`、`--force`（无视禁跑时段）。

和 run.py 一样必须**从 repo 根目录运行**（`.env`、`data/`、`failure/` 都是相对 cwd）。
"""

import json
import os
import re
import shutil
import sys
import time

import polish
import run
from polish import (
    DEFAULT_PROVIDER, PROVIDERS, clean_json_str, make_client,
    select_provider_interactive, try_auto_repair,
)
from schema_check import (
    CHECKED_FIELDS, coerce_fields, strip_unknown_keys, validate_fields,
)
from import_archive import generate_sql

FAILURE_DIR = run.FAILURE_DIR
FIXED_DIR = os.path.join(FAILURE_DIR, "fixed")   # 修好并入库的原文归档处
DATA_DIR = run.DATA_DIR

# 重生成要把整篇档案原样吐回来，预算按 provider 的上限给（思考也算在里面）
REGEN_MAX_TOKENS = 0      # 0 = 用 provider 的 max_tokens

# 重生成后正文相对原文的最低保留比例。低于这个值说明模型把档案缩写了，
# 宁可跳过也不要入一份被砍短的档案。
MIN_CONTENT_RATIO = 0.5

ENABLE_AI = True          # --no-ai 关掉，只跑本地修复（0 token，用来看本地能修多少）
DRY_RUN = False           # --dry-run：不配图、不入库，结果写 data/<stem>_fixed.json

# tb_archive 的语义列（= SYSTEM_PROMPT 产出的字段）。AI 修复时偶尔会顺手多出一个
# 顶层键，generate_sql 是按 dict 的键直接拼列名的，多一个键 = INSERT 报未知列，
# 所以入库前按这张表过滤一遍。
ALLOWED_TOP_LEVEL = {
    "title", "lang", "content", "location", "location_desc",
    "characters", "timelines", "evidence", "ref_links",
    "status", "occurred_at", "closed_at", "tags",
}

REGEN_PROMPT = """你拿到的是一份**已经写好的档案**，但它作为 JSON 是坏的：可能语法不合法
（少引号/少逗号/括号对不上/转义错），也可能结构不合规（字段少了必填键、边引用了不存在的节点）。

你的任务是**把它原样重新输出成一份合法、合规的 JSON**。

## 铁律：内容照抄，不要创作

- `content` 正文**一字不许增删改写**：不许缩写、不许概括、不许续写、不许翻译、不许改标题层级。
  原文有多长就照抄多长。这是一份已经定稿的档案，你只是在修它的 JSON 外壳。
- 其余字段同理：原文里有的人物、时间线、证据、链接**全部保留**，照原样搬过去。
- 只有一种情况可以动内容：某个对象缺了必填键而原文里又找不到依据，
  这时按上下文补一个最简短的合理值（例如缺 `name` 就用原文里出现的称呼）。
- 原文如果在结尾处**被截断**（写到一半没了），把最后那个不完整的对象**整个丢掉**，
  保证 JSON 收尾完整——但**不许自己编内容把它补全**。

## 顶层字段（只许出现这些键，类型必须对）

- `title`：字符串
- `lang`：**数字** `0`（中文）/ `1`（英文）。不许写 `"zh-CN"` 这种字符串
- `content`：字符串（markdown 正文）
- `location`：**PostGIS WKT 字符串** `"POINT(经度 纬度)"`，如 `"POINT(113.2644 23.1291)"`；
  判断不了就填 `null`。**不许把地名写在这里**——地名写 `location_desc`
- `location_desc`：字符串，地点的文字描述，如 `"美国密苏里州某小镇"`
- `characters`、`timelines`、`evidence`、`ref_links`：见下面的结构规范，没有就填 `null`
- `status`：**数字** `0`（未结案）/ `1`（已结案）。不许写 `"closed"`
- `occurred_at`、`closed_at`：ISO8601 字符串，如 `"2023-10-25T14:30:00+00:00"`；不明填 `null`
- `tags`：字符串数组

原文里这些字段要是写错了类型（比如 `lang` 写成 `"zh-CN"`、`location` 写成地名），
**按上面的规范改正**，这不算改内容。

## 结构规范（每个对象只许出现列出的键，一个都不许多）

- `characters`：`{"nodes": [...], "edges": [...]}` 或 null
  - nodes[]：`id`、`name`、`role`、`tags`、`description`（必填 `id`、`name`）
  - edges[]：`source`、`target`、`base_relation`、`interactions`（必填 `source`、`target`）
  - edges[].interactions[]：`action`、`timestamp`、`detail`
- `evidence`：`{"nodes": [...], "edges": [...]}` 或 null
  - nodes[]：`id`、`name`、`type`、`reliability`、`description`、`source`、
    `related_characters`、`related_timelines`（必填 `id`、`name`、`type`）
  - edges[]：`source`、`target`、`relation_type`、`description`、`related_timelines`
- `timelines`：数组或 null，每项 `id`、`time_type`、`timestamp`、`time_display`、
  `title`、`content`、`importance`、`related_characters`、`tags`（必填 `id`、`title`、`content`）
- `ref_links`：数组或 null，每项 `title`、`url`

**枚举值只能取**：`evidence.type` ∈ physical/documentary/testimonial/video/audio；
`reliability` ∈ high/medium/low（没有 critical）；
`relation_type` ∈ corroborates/leads_to/derived_from/contradicts/supports；
`timelines.time_type` ∈ precise/fuzzy；`timelines.importance` ∈ critical/high/normal。

`edges` 里 `source`/`target` 引用的 id **必须真的在同一字段的 `nodes` 里出现过**；
凑不出对应节点的边，直接删掉这条边。

## 输出

只返回那一个 JSON 对象本身，不要解释、不要 markdown 代码块。
"""


# =========================
# 1. 本地无损修复（0 token）
# =========================
_VALID_ESCAPES = set('"\\/bfnrtu')


def sanitize_json_text(raw: str) -> str:
    """逐字符扫一遍，修掉 AI 最常犯的四类破格。都能靠上下文机械判定，所以不花 token：

    1. 字符串里的裸控制字符：真换行 → `\\n`、TAB → `\\t`、CR 与其它控制字符直接丢
    2. 非法转义：`\\x` 这种 JSON 不认的转义，把反斜杠补成 `\\\\` 当字面量反斜杠
    3. **字符串里的裸引号**（`"title": "《老友记》"钱德勒"之死"`）：见到 `"` 时往后
       跳过空白看一眼——后面是 `,` `:` `}` `]` 才是真的收尾，否则它就是正文里的引号，
       转义成 `\\"`。AI 修这类最容易改坏（它会把一个字段劈成两个），本地判反而准
    4. **缺失的逗号**：真收尾的 `"`（或 `]` `}`）后面直接跟下一个 `"` / `{` / `[`，
       中间少了逗号，补上

    第 3 条顺带让 in_string 的跟踪重新准确——裸引号会让「见引号就翻转」的朴素扫描
    从此错位，后面全乱。
    """
    out: list[str] = []
    in_string = False
    i = 0
    n = len(raw)

    while i < n:
        ch = raw[i]

        if not in_string:
            if ch == "\\":
                # 字符串外面不存在合法的反斜杠。AI 偶尔会整段用转义形态写键名
                # （`\"description\": \"...\"`），把这个反斜杠丢掉就还原了
                i += 1
                continue

            out.append(ch)
            if ch == '"':
                in_string = True
            elif ch in "]}":
                # `} "key"` / `] {` 这类缺逗号的断法（空串要单独挡：`"" in x` 恒为真）
                nxt = _next_visible(raw, i + 1)
                if nxt and nxt in '"{[':
                    out.append(",")
            i += 1
            continue

        # ── 字符串内部 ──
        if ch == '"':
            nxt = _next_visible(raw, i + 1)
            if not nxt or nxt in ',:}]':
                in_string = False
                out.append(ch)
            elif nxt in '"{[':
                in_string = False       # 真收尾，只是少了逗号
                out.append('",')
            else:
                out.append('\\"')       # 正文里的引号
            i += 1
            continue

        if ch == "\\":
            nxt = raw[i + 1] if i + 1 < n else ""
            if nxt in _VALID_ESCAPES and not (
                nxt == "u" and not _is_hex4(raw, i + 2)
            ):
                out.append(ch)
                out.append(nxt)
                i += 2
            else:
                out.append("\\\\")      # 非法转义：反斜杠本身当字面量
                i += 1
            continue

        if ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r" or ord(ch) < 0x20:
            pass                        # 其它裸控制字符直接丢
        else:
            out.append(ch)
        i += 1

    return "".join(out)


def _next_visible(s: str, start: int) -> str:
    """往后跳过空白，返回下一个可见字符（到头返回空串）"""
    for ch in s[start:start + 200]:
        if not ch.isspace():
            return ch
    return ""


def _is_hex4(s: str, start: int) -> bool:
    return len(s) >= start + 4 and all(
        c in "0123456789abcdefABCDEF" for c in s[start:start + 4]
    )


def hopeless(cleaned: str, err: json.JSONDecodeError) -> str:
    """AI 修不动的坏法，提前认出来省掉整轮调用。返回原因，空串表示值得一试。

    - 压根不是 JSON：模型无视格式直接吐了 markdown 正文，没有「语法错误」可修
    - 尾部截断：`max_tokens` 砍断的输出，后面的内容根本没生成过，
      让 AI 接着编等于伪造档案，只能拿原文重跑
    """
    if not cleaned.startswith("{"):
        return "输出不是 JSON 对象（AI 直接吐了正文），需要拿原文重跑"

    if err.msg.startswith("Unterminated string") and err.pos > len(cleaned) * 0.9:
        return "输出在结尾处被截断（max_tokens），缺失内容无法修复，需要拿原文重跑"

    return ""


def local_parse(cleaned: str):
    """本地修复链：原样 → sanitize → polish 的自动修复。返回 (dict|None, 说明)"""
    try:
        return json.loads(cleaned), "无需修复"
    except json.JSONDecodeError:
        pass

    fixed = sanitize_json_text(cleaned)
    if fixed != cleaned:
        try:
            return json.loads(fixed), "本地转义修复"
        except json.JSONDecodeError:
            pass

    parsed, how = try_auto_repair(cleaned)
    if parsed is not None:
        return parsed, f"本地修复（{how}）"

    return None, ""


# =========================
# 2. AI 整篇重生成（一次调用，不重试）
# =========================
def read_reason(path: str) -> str:
    """取同名 .reason.txt 里的失败原因；没有就返回空串"""
    reason_path = (path[:-5] if path.endswith(".json") else path) + ".reason.txt"
    if not os.path.exists(reason_path):
        return ""
    try:
        with open(reason_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def regenerate(broken: str, reason: str, client, model, max_tokens,
               extra_body) -> dict:
    """把整篇坏 JSON（有原因就连原因一起）发给 AI，要它吐回一份合法合规的 JSON。

    **只调一次，不重试**：这活儿要么模型一遍就照抄对了，要么它就是想改写正文，
    多试几次只是多烧几倍 token。失败就让调用方跳过这条。
    """
    payload = {"坏掉的档案 JSON": broken}
    if reason:
        # 有原因文件就把它一起给过去：解析器报的错最能指出该往哪儿看
        payload["这份档案当初失败的原因"] = reason

    extra = {"extra_body": extra_body} if extra_body else {}
    if polish.ENABLE_JSON_MODE:
        extra["response_format"] = polish.JSON_RESPONSE_FORMAT

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": REGEN_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        temperature=0.0,
        max_tokens=(REGEN_MAX_TOKENS or max_tokens),
        **extra,
    )

    choice = resp.choices[0]
    raw = choice.message.content or ""
    if not raw.strip():
        raise ValueError(f"AI 返回空内容（finish_reason={choice.finish_reason}）")
    if choice.finish_reason == "length":
        raise ValueError("AI 输出被 max_tokens 截断，重生成的档案不完整")

    parsed, _how = local_parse(clean_json_str(raw))
    if not isinstance(parsed, dict):
        raise ValueError("AI 的响应仍然不是可解析的 JSON 对象")
    return parsed


# =========================
# 3. 结构修复：先本地剪枝，再定点问 AI
# =========================
def _prune_list(items) -> list[str]:
    """就地删掉列表里的 null / 非对象项；返回说明"""
    if not isinstance(items, list):
        return []

    bad = [i for i, x in enumerate(items) if not isinstance(x, dict)]
    for i in reversed(bad):
        items.pop(i)
    return [f"删除 {len(bad)} 个非对象项"] if bad else []


def prune_structure(data: dict) -> list[str]:
    """本地剪枝（0 token）：

    - nodes/edges/timelines/ref_links 里的 null 与非对象项：没有任何信息，删掉
    - 指向不存在节点的悬空边：删掉比让 AI 凭空编一个节点安全（宁可少画一条线）
    """
    notes: list[str] = []

    for field in ("characters", "evidence"):
        graph = data.get(field)
        if not isinstance(graph, dict):
            continue

        for note in _prune_list(graph.get("nodes")):
            notes.append(f"{field}.nodes {note}")
        for note in _prune_list(graph.get("edges")):
            notes.append(f"{field}.edges {note}")

        nodes = graph.get("nodes")
        edges = graph.get("edges")
        if isinstance(nodes, list) and isinstance(edges, list):
            node_ids = {n.get("id") for n in nodes if isinstance(n, dict)}
            dangling = [
                i for i, e in enumerate(edges)
                if isinstance(e, dict)
                and (e.get("source") not in node_ids or e.get("target") not in node_ids)
            ]
            for i in reversed(dangling):
                edges.pop(i)
            if dangling:
                notes.append(f"{field}.edges 删除 {len(dangling)} 条悬空边")

    for field in ("timelines", "ref_links"):
        for note in _prune_list(data.get(field)):
            notes.append(f"{field} {note}")

    return notes


def _locate(data: dict, path: str):
    """按 `characters.nodes[3]` / `timelines[0]` 形态的路径取出对象与它的容器"""
    if "[" not in path:
        return None, None, None

    head, idx = path[:-1].split("[")
    try:
        idx = int(idx)
    except ValueError:
        return None, None, None

    parts = head.split(".")
    container = data.get(parts[0])
    for p in parts[1:]:
        if not isinstance(container, dict):
            return None, None, None
        container = container.get(p)

    if not isinstance(container, list) or not 0 <= idx < len(container):
        return None, None, None

    return container, idx, container[idx]


_POINT_RE = re.compile(
    r"^\s*POINT\s*\(\s*-?\d+(\.\d+)?\s+-?\d+(\.\d+)?\s*\)\s*$", re.I
)
_CLOSED_WORDS = {"closed", "close", "1", "true", "已结案", "已完结", "结案"}


def coerce_scalars(data: dict) -> list[str]:
    """把标量字段掰回 tb_archive 要的形态（0 token）。

    实测 AI 重生成时最爱错这三个：`lang` 写成 `"zh-CN"`、`status` 写成 `"closed"`、
    `location` 直接写地名。前两个是列类型不符，第三个更凶——`generate_sql` 会拼成
    `ST_GeomFromText('美国密苏里州', 4326)`，整条 INSERT 直接报错。
    提示词里已经写清楚了，这里再兜一道，本地能定的就别指望模型。
    """
    notes: list[str] = []

    lang = data.get("lang")
    if not isinstance(lang, int) or isinstance(lang, bool) or lang not in (0, 1):
        sample = (data.get("content") or "")[:2000]
        cjk = sum(1 for c in sample if "一" <= c <= "鿿")
        data["lang"] = 0 if cjk > len(sample) * 0.05 else 1
        notes.append(f"lang {lang!r} → {data['lang']}")

    status = data.get("status")
    if not isinstance(status, int) or isinstance(status, bool) or status not in (0, 1):
        data["status"] = 1 if str(status).strip().lower() in _CLOSED_WORDS else 0
        notes.append(f"status {status!r} → {data['status']}")

    loc = data.get("location")
    if loc is not None and not _POINT_RE.match(str(loc)):
        # 地名挪进 location_desc（那儿空着的话），坐标位置置空
        if not data.get("location_desc"):
            data["location_desc"] = str(loc)
            notes.append(f"location {loc!r} → location_desc")
        else:
            notes.append(f"location {loc!r} 不是 POINT(...) → null")
        data["location"] = None

    return notes


def normalize_local(data: dict) -> list[str]:
    """本地归一 + 剪枝（0 token），返回改动说明。不合规与否由调用方 validate"""
    return (coerce_fields(data) + strip_unknown_keys(data)
            + prune_structure(data) + coerce_scalars(data))


# =========================
# 4. 单个文件的处理
# =========================
def failure_files(exclude: set[str]) -> list[str]:
    """failure/ 下的待修文件（.reason.txt 是说明、fixed/ 是归档，都排除）"""
    if not os.path.isdir(FAILURE_DIR):
        return []
    return sorted(
        os.path.join(FAILURE_DIR, f)
        for f in os.listdir(FAILURE_DIR)
        if not f.endswith(".reason.txt")
        and os.path.isfile(os.path.join(FAILURE_DIR, f))
        and os.path.join(FAILURE_DIR, f) not in exclude
    )


def drop_unknown_top_level(data: dict) -> dict:
    """只保留 tb_archive 的语义列：多一个顶层键 generate_sql 就会拼出未知列"""
    extra = [k for k in data if k not in ALLOWED_TOP_LEVEL]
    if extra:
        print(f"  🔧 丢弃非表列顶层键：{'、'.join(extra)}")
    return {k: v for k, v in data.items() if k in ALLOWED_TOP_LEVEL}


def archive_fixed(path: str):
    """修好并入库的原文挪进 failure/fixed/，连同它的 .reason.txt"""
    os.makedirs(FIXED_DIR, exist_ok=True)
    for src in (path, path[:-5] + ".reason.txt" if path.endswith(".json") else None):
        if src and os.path.exists(src):
            dst = os.path.join(FIXED_DIR, os.path.basename(src))
            try:
                shutil.move(src, dst)
            except OSError as e:
                print(f"  ⚠️  归档 {os.path.basename(src)} 失败：{e}")


def note_failure(path: str, reason: str):
    """修不好就把原因追加进 .reason.txt，文件留在原地等人工处理。

    --dry-run / --no-ai 是试跑，不往 .reason.txt 里记账，免得把「本来就没试」
    写成失败记录污染人工排查的线索。
    """
    if DRY_RUN or not ENABLE_AI:
        return

    reason_path = (path[:-5] if path.endswith(".json") else path) + ".reason.txt"
    with open(reason_path, "a", encoding="utf-8") as f:
        f.write(f"\n{time.strftime('%Y-%m-%d %H:%M:%S')} failure_fix\n{reason}\n")


def call_with_retry(fn, *args, **kwargs):
    """余额不足 / 用量限额时挂起重试（与 run.polish_with_retry 同一套策略）"""
    while True:
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if run.is_quota_error(e):
                run.wait_out_quota(e)
                print("  ▶️  重试中…\n")
                continue
            if not run.is_balance_error(e):
                raise
            print(f"\n  💳 API 余额不足：{e}")
            print("     请充值后按 Enter 重试（Ctrl+C 退出）")
            try:
                input("  > ")
            except EOFError:
                raise
            print("  ▶️  重试中…\n")


def process_file(path: str, client, model, max_tokens, extra_body) -> bool:
    """本地能修就本地修，修不了就让 AI 整篇重生成一次。

    **不重试**：AI 那一次没成、或者结果仍不合规，就跳过这条（原文留在 failure/）。
    返回是否成功入库（--dry-run 下修好即算成功）。
    """
    name = os.path.basename(path)
    stem = os.path.splitext(name)[0]
    print(f"\n{'─' * 56}")
    print(f"  修复：{name}")

    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    cleaned = clean_json_str(raw)

    # ── 1. 先白嫖本地：能解析 + 结构合规就完全不花 token ─────────
    data, how = local_parse(cleaned)
    ok_local = False
    if isinstance(data, dict):
        for note in normalize_local(data):
            print(f"  🔧 本地归一：{note}")
        errors = validate_fields(data)
        if not errors:
            print(f"  ✅ {how}（0 token）")
            ok_local = True
        else:
            print(f"  ⚠️  本地解析成功但结构不合规：{errors[0]}")

    # ── 2. 本地搞不定 → AI 整篇重生成，只调一次 ─────────────────
    if not ok_local:
        if not ENABLE_AI:
            msg = "本地修不了且 --no-ai，跳过"
            print(f"  ⏭️  {msg}")
            note_failure(path, msg)
            return False

        reason = read_reason(path)
        print(f"  🤖 交给 AI 整篇重生成（{len(cleaned)} 字符"
              f"{'，附失败原因' if reason else '，无原因文件'}）…")

        try:
            data = call_with_retry(regenerate, cleaned, reason,
                                   client, model, max_tokens, extra_body)
        except Exception as e:
            print(f"  ❌ 重生成失败：{e}，跳过（不重试）")
            note_failure(path, f"重生成失败：{e}")
            return False

        for note in normalize_local(data):
            print(f"  🔧 本地归一：{note}")

        errors = validate_fields(data)
        if errors:
            msg = "重生成的结果仍不合规：" + "；".join(errors[:3])
            print(f"  ❌ {msg}，跳过（不重试）")
            note_failure(path, msg)
            return False

        # 重生成最大的风险是模型顺手把正文缩写了，长度对比一下就能拦住
        before = len(cleaned)
        after = len(data.get("content") or "")
        if after < before * MIN_CONTENT_RATIO * 0.5:
            msg = (f"重生成后正文只剩 {after} 字（原文件 {before} 字符），"
                   f"疑似被缩写，跳过")
            print(f"  ❌ {msg}")
            note_failure(path, msg)
            return False
        print(f"  ✅ 重生成成功（正文 {after} 字）")

    data = drop_unknown_top_level(data)

    # ── 3. 完整性：拼 SQL 至少要有 title + content ──────────────
    if not data.get("title") or not (data.get("content") or "").strip():
        msg = ("缺少 title 或 content，无法入库"
               "（这是旧版只存四个 jsonb 字段的 failure 文件，"
               "请把源文件重新投进 FILES_TO_SQL/ 跑一遍）")
        print(f"  ❌ {msg}")
        note_failure(path, msg)
        return False

    # ── 4. 配图（原文里已经有图就不重复插）──────────────────────
    if not DRY_RUN and "![" not in data["content"]:
        run.attach_cover_image(data)

    out_path = os.path.join(DATA_DIR, f"{stem}_fixed.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  💾 已写出 {out_path}")

    if DRY_RUN:
        print("  🧪 --dry-run：不入库")
        return True

    # ── 5. 入库（沿用 run.persist_record：先写 SQL 存档再入库）──
    try:
        sql = generate_sql(data)
    except Exception as e:
        print(f"  ❌ 生成 SQL 失败：{e}")
        note_failure(path, f"生成 SQL 失败：{e}")
        return False

    if not run.persist_record(sql, stem):
        note_failure(path, "入库失败，SQL 已留在存档文件里可手工补执行")
        return False

    run.record_done(data, stem)
    archive_fixed(path)
    print(f"  📦 原文已归档 → {FIXED_DIR}/")
    return True


# =========================
# 5. 主循环
# =========================
def main():
    global ENABLE_AI, DRY_RUN

    args = sys.argv[1:]
    once = "--once" in args
    DRY_RUN = "--dry-run" in args
    ENABLE_AI = "--no-ai" not in args
    run.FORCE_BYPASS_PAUSE = "--force" in args

    limit = None
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])

    # 位置参数 = 只修这几个文件（调试单条时用），给了就不进驻留循环
    flag_values = {
        args[args.index(f) + 1]
        for f in ("--limit", "--provider", "--model") if f in args
    }
    only = [
        a for a in args
        if not a.startswith("--") and a not in flag_values
    ]

    provider = DEFAULT_PROVIDER
    if "--provider" in args:
        provider = args[args.index("--provider") + 1]
        if provider not in PROVIDERS:
            print(f"⚠️  未知 provider：{provider}，可选：{', '.join(PROVIDERS)}")
            sys.exit(2)
    elif ENABLE_AI:
        provider = select_provider_interactive(run.config, DEFAULT_PROVIDER)

    client = model = extra_body = None
    max_tokens = 0
    if ENABLE_AI:
        client, model, max_tokens, extra_body = make_client(run.config, provider)
        # 修复是小活，可以指定比主流水线更快/更便宜的模型（如 MiniMax 的 -highspeed）
        if "--model" in args:
            model = args[args.index("--model") + 1]
        print(f"🔌 provider = {provider} ({PROVIDERS[provider]['label']}, model={model})")
    else:
        print("🚫 --no-ai：只跑本地修复")

    os.makedirs(DATA_DIR, exist_ok=True)
    run.session_sql_path = os.path.join(
        DATA_DIR, f"fixed_{time.strftime('%Y%m%d_%H%M%S')}.sql"
    )
    if DRY_RUN:
        print("🧪 --dry-run：只修复不入库")

    done = failed = 0
    handled: set[str] = set()      # 修不好的登记在案，本次运行不再重试（同 run.handled）

    while True:
        pending = only or failure_files(handled)
        if limit is not None:
            pending = pending[:max(0, limit - done - failed)]

        if not pending:
            print(f"\n{'─' * 56}")
            print(f"  📭 {FAILURE_DIR}/ 没有待修文件"
                  f"（本次修复入库 {done} 条，仍失败 {failed} 条）")
            if once or limit is not None or only:
                return
            print("  ⏸  等待新文件投放…（Ctrl+C 退出）")
            while not failure_files(handled):
                time.sleep(run.FILE_POLL_TICK)
            print("  ▶️  检测到新文件，继续\n")
            continue

        for path in pending:
            if ENABLE_AI:
                run.wait_out_pause_window()   # AI 调用同样受禁跑时段约束
            try:
                ok = process_file(path, client, model, max_tokens, extra_body)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"  ❌ 处理异常：{e}")
                note_failure(path, f"处理异常：{e}")
                ok = False

            if ok:
                done += 1
                if DRY_RUN:
                    handled.add(path)   # dry-run 不挪文件，别在循环里反复修同一个
            else:
                failed += 1
                handled.add(path)

        if only:
            print(f"\n  ✔ 指定文件处理完毕（成功 {done}，失败 {failed}）")
            return


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⛔ 已手动中止")
