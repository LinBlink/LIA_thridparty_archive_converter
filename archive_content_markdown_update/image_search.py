"""
百度图片搜索：按档案标题找配图，返回可直接外链的 CDN 地址。

用的是 image.baidu.com 的 acjson 接口（免密钥）。两个必须注意的点：
  1. 必须先访问首页拿到 BAIDUID cookie，否则 data 会随机返回空数组；
  2. 参数必须给全，只传 word/rn 这类精简参数同样会返回空。
返回的 img*.baidu.com/it/u=... 是百度 CDN，实测不校验 Referer，可直接嵌进 markdown。

独立使用：
    python archive_content_markdown_update/image_search.py 红衣男孩
"""

import json
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

TIMEOUT = 15
MIN_WIDTH = 400          # 太小的图当配图不好看，直接过滤
MIN_HEIGHT = 300

# 只认百度自己的图片 CDN：这些域名回 Access-Control-Allow-Origin: *，
# 而 objURL 指向的原始站点普遍既防盗链又不给跨域头，一律不要。
CDN_HOST_RE = re.compile(r"^img\d*\.baidu\.com$", re.I)

CORS_CANDIDATES = 8      # 逐个校验候选图，最多试这么多张

_opener = None            # 复用 opener，cookie 只取一次
_opener_lock = threading.Lock()   # 并发跑批时别让多个线程各建一个、各拿一份 cookie


def _get_opener():
    """建一个带 cookie 的 opener，并访问首页换取 BAIDUID"""
    global _opener
    if _opener is not None:
        return _opener

    with _opener_lock:
        if _opener is not None:      # 等锁期间别的线程已经建好了
            return _opener

        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        req = urllib.request.Request("https://image.baidu.com/", headers={"User-Agent": UA})
        opener.open(req, timeout=TIMEOUT).read()

        _opener = opener
        return _opener


def is_cdn_url(url: str) -> bool:
    """只放行百度图片 CDN 域名（https + img*.baidu.com）"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and bool(CDN_HOST_RE.match(parts.hostname or ""))


def supports_cors(url: str) -> bool:
    """HEAD 一次，确认图片可访问且回了 Access-Control-Allow-Origin（不下载图片本体）"""
    req = urllib.request.Request(
        url,
        method="HEAD",
        headers={
            "User-Agent": UA,
            # 带上 Origin，模拟浏览器跨域请求
            "Origin": "https://example.com",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)
    except Exception:
        return False

    if resp.status != 200:
        return False
    if not (resp.headers.get("Content-Type") or "").startswith("image/"):
        return False

    acao = resp.headers.get("Access-Control-Allow-Origin")
    return acao == "*" or bool(acao)


def _build_url(keyword: str, page_size: int) -> str:
    params = {
        "tn": "resultjson_com", "logid": "1", "ipn": "rj", "ct": 201326592,
        "is": "", "fp": "result", "fr": "",
        "word": keyword, "queryWord": keyword,
        "cl": 2, "lm": -1, "ie": "utf-8", "oe": "utf-8",
        "adpicid": "", "st": -1, "z": "", "ic": 0, "hd": "", "latest": "",
        "copyright": "", "s": "", "se": "", "tab": "", "width": "", "height": "",
        "face": 0, "istype": 2, "qc": "", "nc": 1, "expermode": "", "nojc": "",
        "pn": 0, "rn": page_size, "gsm": "1e",
    }
    return "https://image.baidu.com/search/acjson?" + urllib.parse.urlencode(params)


def search_images(keyword: str, limit: int = 1, page_size: int = 30) -> list[dict]:
    """按关键词搜图，返回 [{"url": ..., "width": ..., "height": ..., "source": ...}]"""
    if not keyword:
        return []

    opener = _get_opener()
    req = urllib.request.Request(
        _build_url(keyword, page_size),
        headers={
            "User-Agent": UA,
            "Referer": "https://image.baidu.com/search/index?tn=baiduimage&word="
                       + urllib.parse.quote(keyword),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    )
    raw = opener.open(req, timeout=TIMEOUT).read().decode("utf-8", "ignore")
    data = json.loads(raw).get("data") or []

    results = []
    for item in data:
        if not item:
            continue
        url = item.get("thumbURL") or item.get("middleURL") or item.get("hoverURL")
        if not url:
            continue
        if (item.get("width") or 0) < MIN_WIDTH or (item.get("height") or 0) < MIN_HEIGHT:
            continue
        if not is_cdn_url(url):
            continue

        results.append({
            "url": url,
            "width": item.get("width"),
            "height": item.get("height"),
            "source": item.get("fromPageTitleEnc") or "",
        })
        if len(results) >= limit:
            break

    return results


def find_cover(keyword: str) -> str | None:
    """
    取一张确认可跨域访问的配图地址。
    任何异常都吞掉返回 None——配图失败不该中断建档。
    """
    try:
        hits = search_images(keyword, limit=CORS_CANDIDATES)
    except Exception:
        return None

    # 逐张 HEAD 校验，返回第一张真的能跨域取到的
    for hit in hits:
        if supports_cors(hit["url"]):
            return hit["url"]

    return None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python archive_content_markdown_update/image_search.py <关键词> [数量]")
        sys.exit(1)

    kw = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    for i, hit in enumerate(search_images(kw, limit=n), 1):
        print(f"{i}. {hit['width']}x{hit['height']}  {hit['url']}")
        if hit["source"]:
            print(f"   来源: {hit['source']}")
