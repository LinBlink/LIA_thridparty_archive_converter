"""
B 站视频定位：把 search.bilibili.com/all?keyword=... 这类「搜索页链接」解析成
真正的视频播放链接（https://www.bilibili.com/video/<BV号>/）。

## 为什么不直接用搜索接口

关键词形如 `【元宝撸奇案】—<视频标题>`，标题往往又长又口语化。实测直接拿它去调
B 站搜索接口，命中率只有 **五成**：接口能返回 20 条结果，但目标视频常常一条都排不进
去——长句分词后关键字散掉，搜出来的全是不相干的东西。往档案里塞错视频比留个搜索链
接更糟，所以不能用。

## 现在的做法：抓 UP 主全量投稿，再本地比对

关键词里的 UP 主名是强信号，而且整库只有 7 个 UP 覆盖了绝大多数记录。于是：

1. `resolve_mid()` 用 UP 名搜一次视频，从结果里挑 author 完全相同的拿到 mid
   （`search_type=bili_user` 匿名调用返回空，不能用）。
2. `fetch_catalog()` 翻 `x/space/wbi/arc/search` 拿这个 UP 的**全部**投稿标题。
3. `Catalog.match()` 用 difflib 在本地比对标题。

搜索引擎的分词问题就此消失，而且 1562 条记录只需要 7 次抓取，不是 1562 次搜索。
抓下来的目录缓存在 `data/bilibili_catalog.json`，重跑直接读缓存。

## 两个必须遵守的接口坑（和 image_search.py 同源）

1. **必须先访问 https://www.bilibili.com/ 拿 buvid3 cookie**，否则一律返回 -412。
   `_get_opener()` 进程内只热身一次。
2. **接口要 wbi 签名**：从 /x/web-interface/nav 取 img_url/sub_url 两段 hash，按固定
   的 MIXIN_KEY_ENC_TAB 重排出 mixin_key，再对参数做 md5 得到 w_rid。签名 key 每天
   变，`_get_mixin_key()` 带 TTL 缓存。

登录不是必须的——匿名（nav 返回 isLogin=False）就能抓。

单独调试：
    python archive_content_markdown_update/bilibili_search.py --build 元宝撸奇案
    python archive_content_markdown_update/bilibili_search.py "<search.bilibili 链接>"
"""

import difflib
import hashlib
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from functools import reduce

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

SEARCH_API = "https://api.bilibili.com/x/web-interface/wbi/search/type"
SPACE_API = "https://api.bilibili.com/x/space/wbi/arc/search"
NAV_API = "https://api.bilibili.com/x/web-interface/nav"

CATALOG_PATH = os.path.join("data", "bilibili_catalog.json")

# 认定「就是这一条」的标题相似度下限。低于它宁可不改。
MATCH_THRESHOLD = 0.72
# 同一个 UP 内，最佳与次佳差距小于它就算「分不清」，一样放弃
AMBIGUITY_MARGIN = 0.04
# 关键词短于这个长度（归一化后）就要求近乎完全一致，否则随便什么标题都能蹭上分
SHORT_TITLE_LEN = 8
SHORT_TITLE_THRESHOLD = 0.95
# 翻页间隔，太快会被 -799 限流
PAGE_INTERVAL = 2.0

_opener = None
_mixin_key = None
_mixin_key_at = 0.0
_MIXIN_TTL = 1800


# ── 底层 HTTP ──────────────────────────────────────────────────


def _get_opener():
    """带 cookie 的 opener，进程内复用；首次构建时访问首页热身拿 buvid3。"""
    global _opener
    if _opener is None:
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        op.addheaders = [
            ("User-Agent", UA),
            ("Referer", "https://www.bilibili.com/"),
            ("Accept", "application/json, text/plain, */*"),
        ]
        try:
            with op.open("https://www.bilibili.com/", timeout=15) as r:
                r.read(2048)
        except Exception:
            pass
        _opener = op
    return _opener


def _get_json(url: str, timeout: int = 20) -> dict:
    with _get_opener().open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def _get_mixin_key() -> str:
    global _mixin_key, _mixin_key_at
    if _mixin_key and time.time() - _mixin_key_at < _MIXIN_TTL:
        return _mixin_key
    wbi = _get_json(NAV_API)["data"]["wbi_img"]
    raw = (wbi["img_url"].rsplit("/", 1)[1].split(".")[0]
           + wbi["sub_url"].rsplit("/", 1)[1].split(".")[0])
    _mixin_key = reduce(lambda s, i: s + raw[i], MIXIN_KEY_ENC_TAB, "")[:32]
    _mixin_key_at = time.time()
    return _mixin_key


def _sign(params: dict) -> str:
    params = dict(params, wts=int(time.time()))
    query = urllib.parse.urlencode(sorted(params.items()))
    params["w_rid"] = hashlib.md5((query + _get_mixin_key()).encode()).hexdigest()
    return urllib.parse.urlencode(sorted(params.items()))


# ── 文本处理 ───────────────────────────────────────────────────

_TAG_RE = re.compile(r"</?em[^>]*>")
_NOISE_RE = re.compile(r"[\s【】\[\]—\-–！!，,。.？?、：:；;“”\"'‘’（）()~～·|/\\]+")


def _clean_title(t: str) -> str:
    """搜索结果标题带 <em class="keyword"> 高亮标签，去掉。"""
    return _TAG_RE.sub("", t or "")


def _norm(t: str) -> str:
    return _NOISE_RE.sub("", t or "")


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def is_search_url(url: str) -> bool:
    return "search.bilibili.com" in (url or "")


def parse_search_url(url: str, known_ups=None):
    """
    从 search.bilibili.com 链接取出 keyword，拆成 (UP主名, 视频标题)。

    关键词形如 `【元宝撸奇案】—【宝哥讲大案】真正的标题…`：**只剥最外层**的
    【】—— 前缀，里层的【邓肯】【宝哥讲大案】通常是视频标题自带的，剥了反而对不上。

    有的关键词只有 `【元宝撸奇案】标题`（没有破折号），还有的 UP 名带错别字
    （元宝撅奇案 / 元宝囒奇案）。传 known_ups 时会拿首个方括号去模糊匹配已知 UP，
    命中就归一化成正名并剥掉——这多救回三百多条本来认不出 UP 的记录。
    """
    query = urllib.parse.urlparse(url).query
    keyword = ""
    for part in query.split("&"):
        if part.startswith("keyword="):
            keyword = urllib.parse.unquote_plus(part[len("keyword="):])
            break
    if not keyword:
        keyword = urllib.parse.unquote_plus(query)
    keyword = keyword.strip()

    m = re.match(r"^【([^】]+)】\s*[—\-–]{1,2}\s*(.+)$", keyword)
    if m:
        head, rest = m.group(1).strip(), m.group(2).strip()
        return _canon_up(head, known_ups), rest

    # 没有破折号：只有当方括号内容像已知 UP 名时才当成 UP 前缀剥掉
    m = re.match(r"^【([^】]+)】\s*(.+)$", keyword)
    if m and known_ups:
        head, rest = m.group(1).strip(), m.group(2).strip()
        canon = _canon_up(head, known_ups, strict=True)
        if canon:
            return canon, rest
    return None, keyword


def _canon_up(name, known_ups, strict: bool = False):
    """把可能带错别字的 UP 名归一到已知 UP 正名；认不出时按 strict 决定返回原名还是 None。"""
    if not name:
        return None
    if not known_ups:
        return None if strict else name
    if name in known_ups:
        return name
    best, best_s = None, 0.0
    for k in known_ups:
        s = _similar(_norm(name), _norm(k))
        if s > best_s:
            best, best_s = k, s
    if best_s >= 0.7:
        return best
    return None if strict else name


# ── 抓取 UP 主投稿目录 ─────────────────────────────────────────


def search_videos(keyword: str, page: int = 1) -> list:
    """搜一页视频，返回原始结果列表；失败返回 []。"""
    url = SEARCH_API + "?" + _sign({
        "search_type": "video", "keyword": keyword, "page": page, "page_size": 20,
    })
    try:
        res = _get_json(url)
    except Exception:
        return []
    if res.get("code") != 0:
        return []
    return res.get("data", {}).get("result") or []


def resolve_mid(up_name: str):
    """
    UP 名 → mid。`search_type=bili_user` 匿名调用返回空 data，所以改成搜视频，
    从结果里挑 author 与 UP 名完全一致的那条取 mid。
    """
    for h in search_videos(up_name):
        if _norm(h.get("author", "")) == _norm(up_name):
            return h.get("mid")
    for h in search_videos(up_name):  # 放宽到包含关系
        a = _norm(h.get("author", ""))
        if a and (a in _norm(up_name) or _norm(up_name) in a):
            return h.get("mid")
    return None


def fetch_catalog(mid: int, verbose: bool = True) -> list:
    """翻页抓一个 UP 的全部投稿，返回 [{bvid, title}]。"""
    out, pn = [], 1
    while True:
        url = SPACE_API + "?" + _sign({
            "mid": mid, "ps": 50, "pn": pn, "order": "pubdate",
            "index": 1, "platform": "web",
        })
        try:
            res = _get_json(url)
        except Exception as e:
            print(f"   ⚠️ 第 {pn} 页抓取失败：{e}")
            break
        if res.get("code") != 0:
            print(f"   ⚠️ 第 {pn} 页返回 code={res.get('code')} {res.get('message')}")
            break
        data = res.get("data", {})
        vlist = (data.get("list") or {}).get("vlist") or []
        if not vlist:
            break
        for v in vlist:
            out.append({"bvid": v.get("bvid"), "title": _clean_title(v.get("title", ""))})
        total = (data.get("page") or {}).get("count", 0)
        if verbose:
            print(f"   … 第 {pn} 页，累计 {len(out)}/{total}")
        if len(out) >= total:
            break
        pn += 1
        time.sleep(PAGE_INTERVAL)
    return out


class Catalog:
    """UP 名 → 投稿列表，带磁盘缓存和本地标题匹配。"""

    def __init__(self, path: str = CATALOG_PATH):
        self.path = path
        self.data = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        # 预算好归一化标题，避免每次匹配都重算
        self._norm_cache = {
            up: [(_norm(v["title"]), v) for v in vids]
            for up, vids in self.data.items()
        }

    @property
    def ups(self):
        return list(self.data.keys())

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)

    def build(self, up_name: str, force: bool = False):
        if up_name in self.data and not force:
            return len(self.data[up_name])
        print(f"🔎 解析 UP「{up_name}」…")
        mid = resolve_mid(up_name)
        if not mid:
            print(f"   ❌ 找不到 mid，跳过")
            return 0
        print(f"   mid={mid}，开始抓投稿")
        vids = fetch_catalog(mid)
        if not vids:
            return 0
        self.data[up_name] = vids
        self._norm_cache[up_name] = [(_norm(v["title"]), v) for v in vids]
        self.save()
        print(f"   ✅ {up_name}: {len(vids)} 个投稿")
        return len(vids)

    def match(self, title: str, up=None):
        """
        在（指定 UP 的 / 全部）投稿里找标题最像的一条。
        返回 (best_video, best_score, runner_up_score)；没有候选时返回 (None, 0, 0)。
        """
        want = _norm(title)
        if not want:
            return None, 0.0, 0.0
        pools = []
        if up and up in self._norm_cache:
            pools = [self._norm_cache[up]]
        else:
            pools = list(self._norm_cache.values())

        scored = []
        for pool in pools:
            for ntitle, v in pool:
                # 先用便宜的长度比过滤，再算 difflib
                if not ntitle:
                    continue
                scored.append((_similar(want, ntitle), v))
        if not scored:
            return None, 0.0, 0.0
        scored.sort(key=lambda x: -x[0])
        best_s, best_v = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        return best_v, best_s, second


# ── 对外主函数 ─────────────────────────────────────────────────


def find_video(search_url: str, catalog: "Catalog" = None):
    """
    把一条 search.bilibili.com 链接解析成最相关的视频。

    返回 dict(bvid, url, title, score, keyword, up, via) —— 只有过线才返回；
    对不上就返回 None（宁可不改）。
    """
    known = catalog.ups if catalog else None
    up, title = parse_search_url(search_url, known)
    if not title:
        return None

    if catalog:
        # 有的关键词退化成只剩 UP 主名（`keyword=Wayne调查`），一点标题信息都没有，
        # 这种拿去比对必然随便撞上一条，直接放弃。
        for k in (known or []):
            if _similar(_norm(title), _norm(k)) >= 0.85:
                return None

        best, score, second = catalog.match(title, up)
        # 关键词太短时相似度不可靠，抬高门槛只认几乎完全一致的
        if best and len(_norm(title)) < SHORT_TITLE_LEN and score < SHORT_TITLE_THRESHOLD:
            return None
        # 全库匹配（认不出 UP）容易撞车，要求最佳明显甩开次佳
        if best and score >= MATCH_THRESHOLD and (up or score - second >= AMBIGUITY_MARGIN):
            return {
                "bvid": best["bvid"],
                "url": f"https://www.bilibili.com/video/{best['bvid']}/",
                "title": best["title"],
                "score": round(score, 3),
                "keyword": title,
                "up": up,
                "via": "catalog",
            }
    return None


def video_embed(url: str) -> str:
    """档案 content 首行用的视频嵌入语法。"""
    return f"@[video]({url})"


# ── 命令行调试 ─────────────────────────────────────────────────

if __name__ == "__main__":
    args = sys.argv[1:]
    cat = Catalog()
    if args and args[0] == "--build":
        for name in args[1:]:
            cat.build(name, force=True)
        sys.exit(0)
    if not args:
        print("用法: python bilibili_search.py --build <UP名>...")
        print("      python bilibili_search.py <search.bilibili 链接>")
        print(f"已缓存 UP：{cat.ups}")
        sys.exit(1)
    hit = find_video(args[0], cat)
    if hit:
        print(f"✅ {hit['score']}  {hit['url']}\n   {hit['title']}")
    else:
        print("❌ 没有足够相似的结果")
