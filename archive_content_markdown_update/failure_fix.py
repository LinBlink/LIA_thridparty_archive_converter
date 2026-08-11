"""failure/ 里的坏档案 → 修复 → 入库。

run.py 里的主流水线不做任何 AI 修复：AI 输出解析不了、或四个 jsonb 字段结构不合规，
原样扔进 `failure/`（原文 `<stem>.json` + 失败原因 `<stem>.reason.txt`）。这个脚本是
那批文件的**离线补救**：修好就补配图、拼 SQL、入库，成功的挪进 `failure/fixed/`。

## 省 token 是第一原则

坏文件动辄一两万字，整篇丢给 AI 重写既贵又容易在重写时改坏正文。所以修复分三档，
**能本地修的绝不调 AI，要调 AI 也只发出问题的那一小段**：

1. **本地无损修**（0 token）：`sanitize_json_text` 带前瞻扫一遍，修四类机械可判的破格
   （裸控制字符、非法转义、字符串里的裸引号、缺失的逗号），再套一遍
   `polish.try_auto_repair`。实测 37 个历史坏文件里 15 个到这一步就修好了。
2. **定点 AI 修语法**（每轮一两千 token）：还解析不了就读 `JSONDecodeError.pos`，
   只截错误处前后 `WINDOW` 个字符、**并从出错点切成两段**发过去，
   模型只回一个 `{"find": ..., "replace": ...}` 最小编辑，**替换由本地做**。
   错一处修一处，最多 `MAX_SYNTAX_ROUNDS` 轮。
3. **结构修**：先本地剪枝（`prune_structure`：删 nodes/edges 里的 null 项、删指向
   不存在节点的悬空边——这两类没有信息量，删掉比让 AI 编一个节点更安全），仍不合规
   才把**出问题的那几个对象**（不是整个字段）发给 AI 重写，最多 `MAX_SCHEMA_ROUNDS` 轮。

`hopeless()` 还会提前认掉 AI 也救不了的两种（压根不是 JSON、尾部被 max_tokens 截断），
直接判失败，省掉整轮调用。

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

WINDOW = 400              # 定点修复时错误位置前后各取多少字符发给 AI
MAX_SYNTAX_ROUNDS = 6     # 语法定点修复的轮数上限（一轮修一处）
MAX_SCHEMA_ROUNDS = 2     # 结构修复的轮数上限
# 定点修复只回一小段（find/replace 两个短串），但**思考也算在 max_tokens 里**：
# MiniMax M2.x 关不掉 thinking，给 4096 会出现思考把预算吃光、content 返回空串的情况。
# 留够余量，反正实际计费按真实生成量走。
REPAIR_MAX_TOKENS = 16384

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

SYNTAX_PROMPT = """你是一个 JSON 语法错误定位器。

用户给你一个 JSON 文档里出错位置附近的两段文字：`出错点之前` 和 `出错点之后`。
把两段直接拼起来就是原文片段（不是完整 JSON，括号不配对是正常的），
**解析器就是在这两段的交界处报的错**，错误原因在 `错误` 里。

你不要重写片段，只要指出这一处该怎么改：

返回格式（只返回这个 JSON 对象）：
{"find": "跨过交界的一小段原文", "replace": "改好后的同一小段"}

规则：
- `find` 必须**跨过那个交界**：从 `出错点之前` 的结尾取十几个字符，接上 `出错点之后`
  开头的十几个字符，一字不差地拼在一起（系统会校验它确实跨过交界，没跨过就白跑一轮）
- `find` 在片段里必须**只出现一次**，长度控制在 20~80 字符
- `replace` 与 `find` 只差那个语法错误：补上缺的逗号/冒号/引号，把字符串里的裸引号写成 \\"、
  裸换行写成 \\n、非法转义的反斜杠写成 \\\\
- **一个字的正文都不许增删改**，只动标点和转义。不要补全片段外的括号，不要输出解释
"""

MAX_EDIT_DELTA = 60      # 一次替换允许的长度变化上限，超了说明 AI 在重写正文而不是补标点

SCHEMA_PROMPT = """你是一个 JSON 结构修复器。

用户给你若干个不合规的对象（来自档案的 characters / timelines / evidence / ref_links 字段），
每个带着它的路径和错误原因。请**只补结构、不要改写内容**：按对象里已有的信息补齐缺失的必填键，
实在推断不出来就用简短的占位文字。

字段规范（每个对象**只许出现下面列出的键，一个都不许多**，后端 Jackson 不认未知键会直接报错）：
- characters.nodes[]：id、name、role、tags、description（必填 id、name）
- characters.edges[]：source、target、base_relation、interactions（必填 source、target）
- evidence.nodes[]：id、name、type、reliability、description、source、related_characters、
  related_timelines（必填 id、name、type；type ∈ physical/documentary/testimonial/video/audio，
  reliability ∈ high/medium/low）
- evidence.edges[]：source、target、relation_type、description、related_timelines
  （relation_type ∈ corroborates/leads_to/derived_from/contradicts/supports）
- timelines[]：id、time_type、timestamp、time_display、title、content、importance、
  related_characters、tags（必填 id、title、content；time_type ∈ precise/fuzzy，
  importance ∈ critical/high/normal）
- ref_links[]：title、url（两个都必填）

返回格式（只返回这个 JSON 对象，键是给你的路径，值是修好的对象）：
{"characters.nodes[3]": {...}, "ref_links[0]": {...}}
文字语言与原值保持一致。
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
# 2. 定点 AI 修语法（只发出错的那一小段）
# =========================
def _ask(client, model, extra_body, system: str, user: str) -> dict:
    """一次非流式小请求。返回空内容 / 回的不是合法 JSON 时重试一次（升点温度换个采样）。

    修复用的响应本身也可能带裸引号（find/replace 里全是引号），所以对它同样先走
    一遍本地修复链再判失败。
    """
    extra = {"extra_body": extra_body} if extra_body else {}
    if polish.ENABLE_JSON_MODE:
        extra["response_format"] = polish.JSON_RESPONSE_FORMAT

    last_err = ""
    for attempt, temp in enumerate((0.0, 0.3), 1):
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temp,
            max_tokens=REPAIR_MAX_TOKENS,
            **extra,
        )
        choice = resp.choices[0]
        raw = choice.message.content or ""

        if not raw.strip():
            last_err = f"AI 返回空内容（finish_reason={choice.finish_reason}）"
        else:
            parsed, _ = local_parse(clean_json_str(raw))
            if isinstance(parsed, dict):
                return parsed
            last_err = "AI 的响应不是可解析的 JSON 对象"

        if attempt == 1:
            print(f"     ⚠️  {last_err}，重试一次")

    raise ValueError(last_err)


def _unescape_pair(find, replace, snippet: str):
    """模型常把 find 写成「多一层转义」的形态（原文是 `"x"`，它回 `\\"x\\"`）——
    它在心里把片段当成 JSON 字符串值又escape 了一遍。逐个试几种去转义，
    谁能在片段里对上就用谁，并**对 replace 施加同一个变换**（两者是同一套写法）。
    """
    if not isinstance(find, str) or not isinstance(replace, str):
        return find, replace

    transforms = (
        lambda s: s,
        lambda s: s.replace('\\"', '"'),
        lambda s: s.replace('\\"', '"').replace("\\\\", "\\"),
    )
    for t in transforms:
        if snippet.count(t(find)) == 1:
            return t(find), t(replace)

    return find, replace


def repair_syntax(text: str, client, model, extra_body) -> dict:
    """一轮修一处：读 `JSONDecodeError.pos`，只把附近 ±WINDOW 个字符发给 AI，
    让它回一个 find/replace 最小编辑，**由本地做替换**。

    为什么不让 AI 直接回「修好的片段」：实测它会顺手把片段重排、掐头去尾，
    一轮就丢掉几百字正文，而且丢了很难发现。改成最小编辑后，AI 碰不到正文，
    replace 长度变化超过 `MAX_EDIT_DELTA` 直接判它越界。

    修好返回 dict；轮数用尽或 AI 没能推进（错误位置不前进）就抛 ValueError。
    """
    original_len = len(text)
    stalled = 0

    for rnd in range(1, MAX_SYNTAX_ROUNDS + 1):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            err = exc            # except 块结束后 exc 会被删掉，先接出来
        else:
            # 兜底体检：最小编辑不该让文档缩水，缩了就是正文被吃掉了
            if len(text) < original_len * 0.95:
                raise ValueError(
                    f"修复后文本从 {original_len} 缩到 {len(text)} 字符，疑似丢正文"
                )
            return parsed

        why = hopeless(text, err)
        if why:
            raise ValueError(why)

        if not ENABLE_AI:
            raise ValueError(f"JSON 解析失败（--no-ai，不调 AI）：{err}")

        start = max(0, err.pos - WINDOW)
        end = min(len(text), err.pos + WINDOW)
        snippet = text[start:end]
        boundary = err.pos - start          # 出错点在片段里的偏移

        print(f"  🤖 定点修复 第 {rnd}/{MAX_SYNTAX_ROUNDS} 轮："
              f"{err.msg} @ {err.pos}/{len(text)}（发送 {len(snippet)} 字符）")

        # 把片段从出错点切成两半发过去：错误就在交界处，模型不用自己数偏移
        payload = json.dumps(
            {
                "错误": err.msg,
                "出错点之前": snippet[:boundary],
                "出错点之后": snippet[boundary:],
            },
            ensure_ascii=False,
        )
        edit = _ask(client, model, extra_body, SYNTAX_PROMPT, payload)
        find, replace = _unescape_pair(edit.get("find"), edit.get("replace"), snippet)
        at = snippet.find(find) if isinstance(find, str) and find else -1

        applied = ""
        if at == -1 or not isinstance(replace, str):
            applied = "AI 未返回能在片段中找到的 find/replace"
        elif snippet.count(find) != 1:
            applied = f"find 在片段中出现 {snippet.count(find)} 次，无法定位"
        elif not at <= boundary <= at + len(find):
            # 上一版没有这道校验，模型经常在附近另找一处「看起来也不对」的地方改，
            # 改完原来的错还在，白烧一轮
            applied = "find 没跨过出错点，拒绝套用"
        elif abs(len(replace) - len(find)) > MAX_EDIT_DELTA:
            applied = (f"replace 与 find 长度差 {len(replace) - len(find)} 字符，"
                       f"超过 {MAX_EDIT_DELTA}，判为改写正文")
        else:
            text = text[:start] + snippet.replace(find, replace, 1) + text[end:]
            print(f"     ✂️  {find[:40]!r} → {replace[:40]!r}")

        if applied:
            print(f"     ⚠️  这一轮没改动：{applied}")

        # 只有「这一轮什么都没改」才算卡住。改动了但错误位置没动是正常的——
        # 同一处可能要补两个字符（先补引号、再补逗号），位置本来就不会前进。
        # 真正跑不动的情况由 MAX_SYNTAX_ROUNDS 兜底。
        if applied:
            stalled += 1
            if stalled >= 2:
                raise ValueError(f"定点修复卡在 {err.pos} 处不前进：{err.msg}")
        else:
            stalled = 0

    raise ValueError(f"定点修复 {MAX_SYNTAX_ROUNDS} 轮后仍解析失败")


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


def _error_paths(errors: list[str]) -> list[str]:
    """从错误信息里抠出 `characters.nodes[3]` 这样的对象路径（去重保序）"""
    paths: list[str] = []
    for err in errors:
        token = err.split(" ")[0].split("=")[0]
        # `characters.edges[1].target` → `characters.edges[1]`
        if "]" in token:
            token = token[:token.index("]") + 1]
        if token.startswith(CHECKED_FIELDS) and token not in paths:
            paths.append(token)
    return paths


def repair_schema(data: dict, client, model, extra_body) -> dict:
    """本地归一 + 剪枝，仍不合规就把**出问题的那几个对象**发给 AI 重写。"""
    for note in coerce_fields(data) + strip_unknown_keys(data) + prune_structure(data):
        print(f"  🔧 本地结构修复：{note}")

    errors = validate_fields(data)

    for rnd in range(1, MAX_SCHEMA_ROUNDS + 1):
        if not errors:
            return data

        if not ENABLE_AI:
            raise ValueError(f"结构不合规（--no-ai，不调 AI）：{'；'.join(errors[:3])}")

        paths = _error_paths(errors)
        targets = {}
        for p in paths:
            _, _, obj = _locate(data, p)
            if obj is not None:
                targets[p] = obj

        if not targets:
            raise ValueError(f"结构不合规且无法定位对象：{'；'.join(errors[:3])}")

        payload = json.dumps(
            {"待修对象": targets, "错误": errors[:len(targets) * 2]},
            ensure_ascii=False,
        )
        print(f"  🤖 结构修复 第 {rnd}/{MAX_SCHEMA_ROUNDS} 轮："
              f"{len(targets)} 个对象（发送 {len(payload)} 字符）")

        repaired = _ask(client, model, extra_body, SCHEMA_PROMPT, payload)
        for path, obj in repaired.items():
            container, idx, _ = _locate(data, path)
            if container is not None and isinstance(obj, dict):
                container[idx] = obj

        strip_unknown_keys(data)     # AI 修完也可能又塞进多余键
        prune_structure(data)
        errors = validate_fields(data)

    if errors:
        raise ValueError(f"结构修复 {MAX_SCHEMA_ROUNDS} 轮后仍不合规：{'；'.join(errors[:3])}")
    return data


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


def process_file(path: str, client, model, extra_body) -> bool:
    """修复 → 校验 → 配图 → 入库。返回是否成功入库（--dry-run 下修好即算成功）"""
    name = os.path.basename(path)
    stem = os.path.splitext(name)[0]
    print(f"\n{'─' * 56}")
    print(f"  修复：{name}")

    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    cleaned = clean_json_str(raw)

    # ── 1. 语法：本地优先，本地修不掉才定点问 AI ────────────────
    data, how = local_parse(cleaned)
    if data is not None:
        print(f"  ✅ {how}（0 token）")
    else:
        try:
            data = call_with_retry(
                repair_syntax, sanitize_json_text(cleaned), client, model, extra_body
            )
            print("  ✅ 定点修复成功")
        except Exception as e:
            print(f"  ❌ 语法修复失败：{e}")
            note_failure(path, f"语法修复失败：{e}")
            return False

    if not isinstance(data, dict):
        print(f"  ❌ 修复结果不是 JSON 对象（{type(data).__name__}）")
        note_failure(path, "修复结果不是 JSON 对象")
        return False

    # ── 2. 结构：本地剪枝 + 定点 AI ─────────────────────────────
    try:
        data = call_with_retry(repair_schema, data, client, model, extra_body)
    except Exception as e:
        print(f"  ❌ 结构修复失败：{e}")
        note_failure(path, f"结构修复失败：{e}")
        return False

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
    if ENABLE_AI:
        client, model, _max_tokens, extra_body = make_client(run.config, provider)
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
                ok = process_file(path, client, model, extra_body)
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
