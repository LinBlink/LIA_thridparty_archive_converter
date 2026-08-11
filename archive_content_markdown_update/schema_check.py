"""jsonb 字段的结构校验（纯本地，不调 AI）。

AI 润色出来的 `characters` / `timelines` / `evidence` / `ref_links` 直接进
`tb_archive` 的 jsonb 列。JSON 本身合法（polish.py 已保证）不代表**结构**对：
常见的破格是把图结构写成裸数组、把整块结构当字符串塞进来、edges 少了
source/target。这些进了库前端就渲染不出来，所以在拼 SQL 之前拦一道。

流程（`ensure_valid_json_fields`）：
  1. 本地无损归一（`coerce_fields` + `strip_unknown_keys`）：字符串形态的 JSON
     解析回来、空容器归 None、白名单外的键剔掉。不花 API 调用。
  2. 还有错就直接抛 `SchemaInvalid`（`polish.ParseFailed` 子类），
     由 run.py 走既有的 `dump_failure` 分支存进 failure/ 后继续下一条。

**不做 AI 修复**：有解码器（`polish.ENABLE_JSON_MODE`）兜底语法、
提示词兜底语义之后，再花一次调用去让 AI 改结构不划算，坏结构一律扔 failure/。
"""

import json

from polish import ParseFailed

# 被校验的四个 jsonb 字段（与 import_archive.JSON_FIELDS 对应）
CHECKED_FIELDS = ("characters", "timelines", "evidence", "ref_links")

# 后端 PO 的字段白名单（yjsws_backend .../domain/po/archive/*.java）。
# CharacterGraph / EvidenceGraph / RefLinks 都**没有** @JsonIgnoreProperties(ignoreUnknown)，
# MyBatis-Plus 的 JacksonTypeHandler 又用默认 ObjectMapper（FAIL_ON_UNKNOWN_PROPERTIES=true），
# 所以多一个键 = 数据写得进库、后端读的时候抛 UnrecognizedPropertyException。
# 只有 Timeline 标了 ignoreUnknown，但也一并收敛，免得脏数据蔓延。
ALLOWED_KEYS = {
    "characters.nodes": {"id", "name", "role", "tags", "description"},
    "characters.edges": {"source", "target", "base_relation", "interactions"},
    "characters.edges.interactions": {"action", "timestamp", "detail"},
    "evidence.nodes": {"id", "name", "type", "reliability", "description",
                       "source", "related_characters", "related_timelines"},
    "evidence.edges": {"source", "target", "relation_type", "description",
                       "related_timelines"},
    "timelines": {"id", "time_type", "timestamp", "time_display", "title",
                  "content", "importance", "related_characters", "tags"},
    "ref_links": {"title", "url"},
}


class SchemaInvalid(ParseFailed):
    """四个 jsonb 字段结构不合规（本地归一后仍不合规，不再尝试修复）"""


# =========================
# 1. 校验
# =========================
def _check_obj_list(value, path: str, required: tuple[str, ...]) -> list[str]:
    """校验「对象数组」：每个元素是 dict，且含 required 里的非空键"""
    errs: list[str] = []
    if not isinstance(value, list):
        return [f"{path} 应为数组，实为 {type(value).__name__}"]

    for i, item in enumerate(value):
        if not isinstance(item, dict):
            errs.append(f"{path}[{i}] 应为对象，实为 {type(item).__name__}")
            continue
        missing = [k for k in required if not item.get(k)]
        if missing:
            errs.append(f"{path}[{i}] 缺少 {'/'.join(missing)}")

    return errs


def _check_graph(value, path: str,
                 node_req: tuple[str, ...],
                 edge_req: tuple[str, ...]) -> list[str]:
    """校验 {nodes: [...], edges: [...]} 图结构；edges 允许缺省（空图）"""
    if not isinstance(value, dict):
        return [f"{path} 应为含 nodes/edges 的对象，实为 {type(value).__name__}"]

    if "nodes" not in value:
        return [f"{path} 缺少 nodes"]

    errs = _check_obj_list(value["nodes"], f"{path}.nodes", node_req)

    edges = value.get("edges")
    if edges is not None:
        errs += _check_obj_list(edges, f"{path}.edges", edge_req)

    # 边引用的节点必须存在，否则前端画不出线
    node_ids = {
        n.get("id") for n in value["nodes"]
        if isinstance(n, dict) and n.get("id")
    } if isinstance(value["nodes"], list) else set()
    if isinstance(edges, list):
        for i, e in enumerate(edges):
            if not isinstance(e, dict):
                continue
            for side in ("source", "target"):
                ref = e.get(side)
                if ref and ref not in node_ids:
                    errs.append(
                        f"{path}.edges[{i}].{side} = {ref!r} 在 nodes 中不存在"
                    )

    return errs


def validate_fields(data: dict) -> list[str]:
    """返回全部结构问题；空列表表示四个字段都合规（None 视为合规）"""
    errs: list[str] = []

    characters = data.get("characters")
    if characters is not None:
        errs += _check_graph(
            characters, "characters",
            node_req=("id", "name"),
            edge_req=("source", "target"),
        )

    evidence = data.get("evidence")
    if evidence is not None:
        errs += _check_graph(
            evidence, "evidence",
            node_req=("id", "name", "type"),
            edge_req=("source", "target"),
        )

    timelines = data.get("timelines")
    if timelines is not None:
        errs += _check_obj_list(
            timelines, "timelines", required=("id", "title", "content"),
        )

    ref_links = data.get("ref_links")
    if ref_links is not None:
        errs += _check_obj_list(
            ref_links, "ref_links", required=("title", "url"),
        )

    return errs


def invalid_fields(errors: list[str]) -> list[str]:
    """从错误信息里反查出问题的字段名，只把这些字段交给 AI 重写"""
    return [f for f in CHECKED_FIELDS if any(e.startswith(f) for e in errors)]


# =========================
# 2. 本地无损归一
# =========================
def _strip_keys(items, path: str) -> list[str]:
    """就地剔除对象数组里不在白名单上的键，返回 ["路径 多余键 (次数)"] 形式的说明"""
    if not isinstance(items, list):
        return []

    allowed = ALLOWED_KEYS[path]
    dropped: dict[str, int] = {}
    notes: list[str] = []

    for item in items:
        if not isinstance(item, dict):
            continue

        for key in [k for k in item if k not in allowed]:
            item.pop(key)
            dropped[key] = dropped.get(key, 0) + 1

        # characters.edges[].interactions 是唯一的第二层对象数组
        nested = f"{path}.interactions"
        if nested in ALLOWED_KEYS:
            notes += _strip_keys(item.get("interactions"), nested)

    if dropped:
        detail = "、".join(f"{k}×{n}" for k, n in dropped.items())
        notes.insert(0, f"{path} 剔除多余键：{detail}")

    return notes


def strip_unknown_keys(data: dict) -> list[str]:
    """按后端 PO 的白名单剔除多余键。

    这是硬约束不是风格问题：多一个键后端就读不出这份档案，
    所以本地直接删，不值得为此再跑一次 AI 修复。
    """
    notes: list[str] = []

    for field in ("characters", "evidence"):
        graph = data.get(field)
        if isinstance(graph, dict):
            notes += _strip_keys(graph.get("nodes"), f"{field}.nodes")
            notes += _strip_keys(graph.get("edges"), f"{field}.edges")

    notes += _strip_keys(data.get("timelines"), "timelines")
    notes += _strip_keys(data.get("ref_links"), "ref_links")

    return notes


def coerce_fields(data: dict) -> list[str]:
    """不花 API 调用的修复：字符串形态的 JSON 解析回来、空容器归 None。
    返回做过的改动说明，供打印。"""
    fixed: list[str] = []

    for field in CHECKED_FIELDS:
        if field not in data:
            continue
        value = data[field]

        # AI 偶尔把整块结构当字符串塞进来
        if isinstance(value, str):
            text = value.strip()
            if not text or text.lower() in ("null", "none"):
                data[field] = None
                fixed.append(f"{field}: 空字符串 → null")
                continue
            try:
                data[field] = json.loads(text)
                fixed.append(f"{field}: 字符串 → JSON")
                value = data[field]
            except json.JSONDecodeError:
                continue

        # 空容器等价于「没有」，别让它掉进 nodes 缺失的报错里
        if value == {} or value == []:
            data[field] = None
            fixed.append(f"{field}: 空值 → null")

    return fixed


# =========================
# 3. 入口
# =========================
def ensure_valid_json_fields(data: dict) -> dict:
    """本地归一后校验四个 jsonb 字段，不合规直接判失败（不调 AI 修复）。

    就地改写 data 并返回它；不合规抛 SchemaInvalid，交由 run.py 存 failure/。
    """
    for note in coerce_fields(data) + strip_unknown_keys(data):
        print(f"  🔧 结构归一：{note}")

    errors = validate_fields(data)
    if not errors:
        return data

    targets = invalid_fields(errors)
    print(f"  ⚠️  jsonb 字段结构不合规（{'、'.join(targets)}），不修复，直接判失败")
    for e in errors[:5]:
        print(f"       - {e}")
    if len(errors) > 5:
        print(f"       - …共 {len(errors)} 处")

    # raw 存**整条记录**而不是只存四个字段：failure/ 里的文件要能被
    # failure_fix.py 修完直接入库，只有四个 jsonb 字段的话缺 title/content 拼不出 SQL。
    raise SchemaInvalid(
        "jsonb 字段结构不合规：" + "；".join(errors[:5]),
        json.dumps(data, ensure_ascii=False, indent=2),
    )
