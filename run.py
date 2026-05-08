#!/usr/bin/env python3
import asyncio
import html
import json
import os
import re
import socket
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from dateutil import parser as dt_parser

BJ_TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).parent
DOCS = ROOT / "docs"
RUNS = DOCS / "runs"
DEBUG_LOG_PATH = Path("/Users/zzm/.cursor/debug-0bdcc8.log")
DEBUG_SESSION_ID = "0bdcc8"
FETCH_HOME_META: Dict[str, str] = {}
DEFAULT_WSJ_RSS_FEEDS = [
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "https://feeds.a.dj.com/rss/RSSWorldNews.xml",
    "https://feeds.a.dj.com/rss/RSSWSJD.xml",
    "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml",
]


def _sanitize_fetch_snippet(text: str) -> str:
    if not text:
        return ""
    t = re.sub(r"'cookie'\s*:\s*'[^']*'", "'cookie':'[redacted]'", text, flags=re.I)
    t = re.sub(r'"cookie"\s*:\s*"[^"]*"', '"cookie":"[redacted]"', t, flags=re.I)
    return t[:800]


def _is_cn_home(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.netloc.endswith("cn.wsj.com") and p.path in ("", "/")
    except Exception:
        return False


def _dbg(run_id: str, hypothesis_id: str, location: str, message: str, data: dict) -> None:
    payload = {
        "sessionId": DEBUG_SESSION_ID,
        "id": f"log_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}",
        "timestamp": int(time.time() * 1000),
        "location": location,
        "message": message,
        "data": data,
        "runId": run_id,
        "hypothesisId": hypothesis_id,
    }
    with DEBUG_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


@dataclass
class ModelConfig:
    base_url: str
    api_key: str
    model: str


def now_bj() -> datetime:
    return datetime.now(BJ_TZ)


def get_model(prefix: str) -> Optional[ModelConfig]:
    base = os.getenv(f"{prefix}_BASE_URL", "").strip()
    key = os.getenv(f"{prefix}_API_KEY", "").strip()
    model = os.getenv(f"{prefix}_MODEL", "").strip()
    if base and key and model:
        return ModelConfig(base, key, model)
    return None


def normalize_url(base: str) -> str:
    b = base.rstrip("/")
    return b if b.endswith("/chat/completions") else f"{b}/chat/completions"


def load_netscape_cookies(run_id: str = "runtime") -> Dict[str, str]:
    raw_path = os.getenv("COOKIE_FILE", "").strip()
    jar: Dict[str, str] = {}
    if not raw_path:
        # #region agent log
        _dbg(run_id, "H1", "run.py:load_netscape_cookies", "COOKIE_FILE not set", {})
        # #endregion
        return jar
    cookie_file = Path(raw_path).expanduser()
    total_rows = 0
    expirable_rows = 0
    expired_rows = 0
    now_ts = int(time.time())
    name_list: List[str] = []
    if not cookie_file.is_file():
        # #region agent log
        _dbg(
            run_id,
            "H1",
            "run.py:load_netscape_cookies",
            "cookie file missing or not a file",
            {"cookie_file": str(cookie_file)},
        )
        # #endregion
        return jar
    for line in cookie_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        raw = line.strip()
        if not raw:
            continue
        if raw.startswith("#HttpOnly_"):
            raw = raw[len("#HttpOnly_") :]
        elif raw.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) >= 7 and parts[5].strip():
            total_rows += 1
            try:
                exp = int(parts[4].strip())
                if exp > 0:
                    expirable_rows += 1
                    if exp < now_ts:
                        expired_rows += 1
            except Exception:
                pass
            nm = parts[5].strip()
            name_list.append(nm)
            jar[nm] = parts[6].strip()
    critical_names = ["wsjregion", "DJSESSIONID", "djcs_route", "djcs_oauth", "cX_P"]
    critical_presence = {k: (k in jar) for k in critical_names}
    dup_counts = Counter(name_list)
    dup_names = [n for n, c in dup_counts.items() if c > 1]
    # #region agent log
    _dbg(
        run_id,
        "H1",
        "run.py:load_netscape_cookies",
        "cookie file parsed",
        {
            "cookie_file": str(cookie_file),
            "cookie_count": len(jar),
            "total_rows": total_rows,
            "expirable_rows": expirable_rows,
            "expired_rows": expired_rows,
            "critical_presence": critical_presence,
            "duplicate_name_count": len(dup_names),
            "duplicate_names_sample": dup_names[:15],
        },
    )
    # #endregion
    return jar


def load_playwright_cookies() -> List[dict]:
    raw_path = os.getenv("COOKIE_FILE", "").strip()
    if not raw_path:
        return []
    cookie_file = Path(raw_path).expanduser()
    out: List[dict] = []
    if not cookie_file.is_file():
        return out
    for line in cookie_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        raw = line.strip()
        if not raw:
            continue
        if raw.startswith("#HttpOnly_"):
            raw = raw[len("#HttpOnly_") :]
        elif raw.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) < 7:
            continue
        domain, _, path, secure, _, name, value = parts[:7]
        if not name.strip():
            continue
        dom = domain.strip()
        if dom.startswith("."):
            dom = dom[1:]
        out.append(
            {
                "name": name.strip(),
                "value": value.strip(),
                "domain": dom or "cn.wsj.com",
                "path": path or "/",
                "secure": secure.upper() == "TRUE",
                "sameSite": "None",
            }
        )
    return out


def fetch_html(url: str, cookies: Dict[str, str], run_id: str = "runtime") -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    with httpx.Client(timeout=25, follow_redirects=True) as client:
        r = client.get(url, headers=headers, cookies=cookies)
        # #region agent log
        _dbg(
            run_id,
            "H2",
            "run.py:fetch_html",
            "http fetch response",
            {"url": url, "status_code": r.status_code, "cookie_count": len(cookies)},
        )
        # #endregion
        if r.status_code in (401, 403):
            # #region agent log
            raw_snip = re.sub(r"\s+", " ", (r.text or "")[:1200])
            snippet = _sanitize_fetch_snippet(raw_snip)
            if _is_cn_home(url):
                FETCH_HOME_META["http_401_body"] = snippet
            _dbg(
                run_id,
                "H6",
                "run.py:fetch_html",
                "http 401/403 body snippet",
                {"url": url, "snippet": snippet},
            )
            # #endregion
            return fetch_html_playwright(url, cookies, run_id)
        r.raise_for_status()
        return r.text


def fetch_html_playwright(url: str, cookies: Dict[str, str], run_id: str = "runtime") -> str:
    async def _go() -> str:
        from playwright.async_api import async_playwright
        try:
            from playwright_stealth import Stealth
            manager = Stealth().use_async(async_playwright())
        except Exception:
            manager = async_playwright()

        async with manager as p:
            launch_args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
            use_ch = os.getenv("PLAYWRIGHT_USE_CHROME_CHANNEL", "1").lower() in ("1", "true", "yes")
            browser = None
            if use_ch:
                try:
                    browser = await p.chromium.launch(headless=True, channel="chrome", args=launch_args)
                    # #region agent log
                    _dbg(run_id, "H7", "run.py:fetch_html_playwright", "playwright launch", {"channel": "chrome"})
                    # #endregion
                except Exception as le:
                    # #region agent log
                    _dbg(
                        run_id,
                        "H7",
                        "run.py:fetch_html_playwright",
                        "playwright chrome channel failed",
                        {"error": str(le)[:200]},
                    )
                    # #endregion
                    browser = None
            if browser is None:
                browser = await p.chromium.launch(headless=True, args=launch_args)
                # #region agent log
                _dbg(run_id, "H7", "run.py:fetch_html_playwright", "playwright launch", {"channel": "chromium"})
                # #endregion
            storage_path = os.getenv("PLAYWRIGHT_STORAGE_STATE", "").strip()
            base_ctx = dict(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1366, "height": 900},
            )
            if storage_path and Path(storage_path).expanduser().exists():
                sp = str(Path(storage_path).expanduser())
                # #region agent log
                _dbg(run_id, "H7", "run.py:fetch_html_playwright", "using playwright storage_state", {"path": sp})
                # #endregion
                ctx = await browser.new_context(**base_ctx, storage_state=sp)
            else:
                ctx = await browser.new_context(**base_ctx)
            await ctx.set_extra_http_headers(
                {
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Referer": "https://cn.wsj.com/",
                }
            )
            if not (storage_path and Path(storage_path).expanduser().exists()):
                pw_cookies = load_playwright_cookies()
                if pw_cookies:
                    await ctx.add_cookies(pw_cookies)
                elif cookies:
                    await ctx.add_cookies(
                        [
                            {
                                "name": k,
                                "value": v,
                                "domain": "cn.wsj.com",
                                "path": "/",
                                "secure": True,
                                "sameSite": "None",
                            }
                            for k, v in cookies.items()
                        ]
                    )
            page = await ctx.new_page()
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # #region agent log
            _dbg(
                run_id,
                "H2",
                "run.py:fetch_html_playwright",
                "playwright goto response",
                {"url": url, "status": int(resp.status) if resp else None},
            )
            # #endregion
            if resp and resp.status >= 400:
                text_snip = ""
                try:
                    raw_html = await page.content()
                    text_snip = BeautifulSoup(raw_html, "html.parser").get_text(" ", strip=True)
                    text_snip = _sanitize_fetch_snippet(re.sub(r"\s+", " ", text_snip)[:1200])
                except Exception as pe:
                    text_snip = f"(could not read page: {pe})"
                # #region agent log
                _dbg(
                    run_id,
                    "H6",
                    "run.py:fetch_html_playwright",
                    "playwright error page text snippet",
                    {"url": url, "status": int(resp.status), "snippet": text_snip},
                )
                # #endregion
                raise RuntimeError(f"playwright status={resp.status}")
            await asyncio.sleep(3)
            html = await page.content()
            await browser.close()
            return html

    return asyncio.run(_go())


def network_preflight(run_id: str) -> None:
    checks = []
    for host in ("cn.wsj.com", "geo.captcha-delivery.com"):
        item = {"host": host, "resolve_ok": False, "tcp_ok": False, "error": ""}
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            item["resolve_ok"] = bool(infos)
        except Exception as exc:
            item["error"] = f"resolve:{exc}"
            checks.append(item)
            continue
        try:
            with socket.create_connection((host, 443), timeout=4):
                item["tcp_ok"] = True
        except Exception as exc:
            item["error"] = f"tcp:{exc}"
        checks.append(item)
    # #region agent log
    _dbg(run_id, "H12", "run.py:network_preflight", "network preflight", {"checks": checks})
    # #endregion


def parse_bj(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = dt_parser.parse(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(BJ_TZ)
    except Exception:
        return None


def brief_window_hours() -> int:
    raw = os.getenv("BRIEF_WINDOW_HOURS", "24").strip()
    try:
        h = int(raw)
        return max(1, min(24 * 14, h))
    except Exception:
        return 24


def in_brief_window(published: Optional[datetime], now: datetime, hours: int) -> bool:
    if not published:
        return False
    return now - timedelta(hours=hours) <= published <= now


def allow_undated_articles() -> bool:
    raw = os.getenv("ALLOW_UNDATED_ARTICLES", "1").strip().lower()
    return raw in ("1", "true", "yes", "on")


def max_undated_articles() -> int:
    raw = os.getenv("MAX_UNDATED_ARTICLES", "5").strip()
    try:
        v = int(raw)
        return max(0, min(30, v))
    except Exception:
        return 5


def _xml_local_name(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def canonical_wsj_article_url(url: str) -> str:
    u = (url or "").split("?")[0].strip()
    if "/amp/articles/" in u:
        u = u.replace("/amp/articles/", "/articles/")
    return u.rstrip("/")


def _is_cn_article_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.netloc.endswith("cn.wsj.com") and "/articles/" in p.path
    except Exception:
        return False


def _allow_non_cn_wsj_rss() -> bool:
    raw = os.getenv("ALLOW_NON_CN_WSJ_RSS", "1").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _is_wsj_article_url(url: str) -> bool:
    try:
        p = urlparse(url)
        host = p.netloc.lower()
        if not host.endswith("wsj.com"):
            return False
        path = (p.path or "").lower()
        # Prefer article-like URLs; keep broad fallback for wsj RSS links.
        return "/articles/" in path or path.count("/") >= 2
    except Exception:
        return False


def html_fragment_to_text(fragment: str) -> str:
    if not fragment:
        return ""
    soup = BeautifulSoup(fragment, "html.parser")
    t = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", t).strip()


def rss_env_feed_urls() -> List[str]:
    raw = os.getenv("WSJ_RSS_URLS", os.getenv("RSS_URLS", "")).strip()
    if raw:
        return [x.strip() for x in raw.split(",") if x.strip()]
    # Default to the same official WSJ feeds used historically in openclaw.
    return list(DEFAULT_WSJ_RSS_FEEDS)


def fetch_rss_xml(feed_url: str, run_id: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CN-WSJ-Briefing/1.0)"}
    with httpx.Client(timeout=40, follow_redirects=True) as client:
        r = client.get(feed_url, headers=headers)
        # #region agent log
        _dbg(
            run_id,
            "H13",
            "run.py:fetch_rss_xml",
            "rss fetch",
            {"url": feed_url, "status": r.status_code, "bytes": len(r.content or b"")},
        )
        # #endregion
        r.raise_for_status()
        return r.text


def parse_rss_items(xml_text: str, run_id: str, feed_url: str) -> List[Tuple[str, dict]]:
    out: List[Tuple[str, dict]] = []
    try:
        root = ET.fromstring(xml_text)
    except Exception as exc:
        _dbg(run_id, "H13", "run.py:parse_rss_items", "rss xml parse failed", {"feed": feed_url, "error": str(exc)[:200]})
        return out
    tag0 = _xml_local_name(root.tag).lower()

    def add_item(link: str, title: str, pub_raw: str, desc_html: str, enc_html: str) -> None:
        link_u = link.strip()
        if not link_u or not title.strip():
            return
        if not link_u.startswith("http"):
            return
        canon = canonical_wsj_article_url(link_u)
        if _is_cn_article_url(canon):
            pass
        elif _allow_non_cn_wsj_rss() and _is_wsj_article_url(canon):
            pass
        else:
            return
        try:
            slug = urlparse(canon).path.rstrip("/").rsplit("/", 1)[-1]
        except Exception:
            slug = ""
        if not slug or slug.startswith("-"):
            return
        body_html = enc_html.strip() if enc_html.strip() else desc_html
        hint = {
            "title": title.strip(),
            "published_raw": pub_raw.strip(),
            "published_bj": parse_bj(pub_raw) if pub_raw.strip() else None,
            "description_html": body_html.strip(),
            "rss_feed": feed_url,
        }
        out.append((canon, hint))

    if tag0 == "rss":
        for item in root.findall(".//item"):
            title = ""
            link = ""
            pub_raw = ""
            desc_html = ""
            enc_html = ""
            for child in item:
                ln = _xml_local_name(child.tag).lower()
                if ln == "title":
                    title = "".join(child.itertext()).strip()
                elif ln == "link":
                    link = (child.text or "").strip()
                elif ln == "pubdate":
                    pub_raw = (child.text or "").strip()
                elif ln == "description":
                    desc_html = "".join(child.itertext()).strip()
                elif ln.endswith("encoded") or ln == "encoded":
                    enc_html = "".join(child.itertext()).strip()
                elif ln == "guid":
                    gt = child.get("isPermaLink", "").lower()
                    gv = (child.text or "").strip()
                    if gt == "false" or (gv.startswith("http") and "wsj.com" in gv):
                        link = link or gv
            add_item(link or "", title, pub_raw, desc_html, enc_html)
        _dbg(run_id, "H13", "run.py:parse_rss_items", "rss items parsed", {"feed": feed_url, "items": len(out)})
        return out

    if tag0 == "feed":
        atom = "{http://www.w3.org/2005/Atom}"
        for entry in root.findall(f"{atom}entry"):
            title_el = entry.find(f"{atom}title")
            title = "".join(title_el.itertext()).strip() if title_el is not None else ""
            link = ""
            for ln in entry.findall(f"{atom}link"):
                href = (ln.get("href") or "").strip()
                if not href:
                    continue
                rel = (ln.get("rel") or "alternate").lower()
                if rel in ("alternate", "self", ""):
                    link = href
                    break
            if not link:
                for ln in entry.findall(f"{atom}link"):
                    href = (ln.get("href") or "").strip()
                    if href:
                        link = href
                        break
            pub_raw = ""
            for tag in ("updated", "published"):
                pe = entry.find(f"{atom}{tag}")
                if pe is not None and (pe.text or "").strip():
                    pub_raw = (pe.text or "").strip()
                    break
            summary_el = entry.find(f"{atom}summary")
            summary_txt = "".join(summary_el.itertext()).strip() if summary_el is not None else ""
            content_el = entry.find(f"{atom}content")
            content_txt = "".join(content_el.itertext()).strip() if content_el is not None else ""
            add_item(link or "", title, pub_raw, summary_txt, content_txt)
        _dbg(run_id, "H13", "run.py:parse_rss_items", "atom items parsed", {"feed": feed_url, "items": len(out)})
        return out

    _dbg(run_id, "H13", "run.py:parse_rss_items", "rss unknown root", {"feed": feed_url, "root": tag0})
    return out


def rss_discover(feed_urls: List[str], run_id: str) -> Dict[str, dict]:
    by_url: Dict[str, dict] = {}
    for fu in feed_urls:
        try:
            xml_txt = fetch_rss_xml(fu, run_id)
            for canon, hint in parse_rss_items(xml_txt, run_id, fu):
                if canon not in by_url:
                    by_url[canon] = hint
        except Exception as exc:
            print(f"[DEBUG] rss feed failed ({fu}): {exc}")
            _dbg(run_id, "H13", "run.py:rss_discover", "rss feed exception", {"feed": fu, "error": str(exc)[:240]})
    return by_url


def article_links(cookies: Dict[str, str], run_id: str = "runtime") -> List[str]:
    try:
        html = fetch_html("https://cn.wsj.com/", cookies, run_id)
    except Exception as exc:
        # #region agent log
        _dbg(run_id, "H2", "run.py:article_links", "homepage fetch exception", {"error": str(exc)})
        # #endregion
        print(f"[DEBUG] homepage fetch failed: {exc}")
        return []
    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    out = []
    for a in soup.select("a[href*='/articles/']"):
        href = (a.get("href") or "").split("?")[0].strip()
        if not href:
            continue
        if href.startswith("/"):
            href = "https://cn.wsj.com" + href
        if not href.startswith("http"):
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append(href)
    # #region agent log
    _dbg(run_id, "H2", "run.py:article_links", "homepage links extracted", {"count": len(out)})
    # #endregion
    return out


def article_from_rss_hint(url: str, hint: dict, run_id: str) -> Optional[dict]:
    body_html = hint.get("description_html") or ""
    body = html_fragment_to_text(body_html)
    if len(body) < 60:
        # #region agent log
        _dbg(run_id, "H14", "run.py:article_from_rss_hint", "rss body too short", {"url": url, "chars": len(body)})
        # #endregion
        return None
    title = (hint.get("title") or "").strip()
    if not title:
        title = "（RSS 未提供标题）"
    lede_txt = body[:260] + ("…" if len(body) > 260 else "")
    pub = hint.get("published_bj")
    if not pub:
        pub = parse_bj((hint.get("published_raw") or "").strip())
    # #region agent log
    _dbg(
        run_id,
        "H14",
        "run.py:article_from_rss_hint",
        "article built from rss",
        {"url": url, "chars": len(body), "has_pub": bool(pub)},
    )
    # #endregion
    return {
        "url": url,
        "title": title,
        "lede": lede_txt.strip() if lede_txt.strip() else "（由 RSS/HTML 摘录生成导语）",
        "image_url": "",
        "published_bj": pub,
        "body": body[:12000],
        "body_source": "rss",
    }


def parse_article(url: str, cookies: Dict[str, str], run_id: str = "runtime") -> Optional[dict]:
    try:
        html = fetch_html(url, cookies, run_id)
        soup = BeautifulSoup(html, "html.parser")
        title = (soup.select_one("meta[property='og:title']") or {}).get("content", "").strip()
        if not title and soup.title:
            title = soup.title.get_text(strip=True)
        lede = (soup.select_one("meta[name='description']") or {}).get("content", "").strip()
        image = (soup.select_one("meta[property='og:image']") or {}).get("content", "").strip()
        if not title:
            return None

        published = None
        modified = None
        for n in soup.select("script[type='application/ld+json']"):
            raw = n.string or n.get_text()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            items = payload if isinstance(payload, list) else [payload]
            for item in items:
                if isinstance(item, dict) and item.get("@type") in {"NewsArticle", "Article"}:
                    published = published or item.get("datePublished")
                    modified = modified or item.get("dateModified")

        body_parts = [p.get_text(" ", strip=True) for p in soup.select("article p")]
        if not body_parts:
            body_parts = [p.get_text(" ", strip=True) for p in soup.select("p")]
        body = "\n".join([x for x in body_parts if x and len(x) > 20])[:12000]
        if not body:
            body = (lede or title or "").strip()
        if not body:
            # #region agent log
            _dbg(run_id, "H3", "run.py:parse_article", "article dropped: empty body", {"url": url, "title": title})
            # #endregion
            return None
        pub = parse_bj(published) or parse_bj(modified)
        # #region agent log
        _dbg(
            run_id,
            "H3",
            "run.py:parse_article",
            "article parsed",
            {"url": url, "has_title": bool(title), "has_lede": bool(lede), "has_body": bool(body), "has_pub": bool(pub)},
        )
        # #endregion
        return {
            "url": url,
            "title": title,
            "lede": lede or "（站点未提供明确导语）",
            "image_url": image,
            "published_bj": pub,
            "body": body,
            "body_source": "site",
        }
    except Exception as exc:
        print(f"[DEBUG] parse_article failed: {url} -> {exc}")
        return None


def _fallback_from_article(article: dict) -> dict:
    body = (article.get("body") or "").strip()
    chunks = [s.strip() for s in re.split(r"[。！？\n]+", body) if len(s.strip()) > 20]
    key_points = chunks[:5] if chunks else ["正文过短，无法提炼要点。"]
    lead = (article.get("lede") or "").strip()
    parts = []
    if lead:
        parts.append(lead.rstrip("。") + "。")
    for c in chunks[:3]:
        if c and c not in lead:
            parts.append(c + ("。" if not c.endswith(("。", "！", "？")) else ""))
        if sum(len(p) for p in parts) > 180:
            break
    ai_summary = "".join(parts)[:280].strip()
    if len(ai_summary) < 50 and chunks:
        ai_summary = chunks[0][:280]
    return {
        "ai_summary": ai_summary,
        "key_points": key_points,
        "risks": ["以下为基于正文切分的摘录，非模型推理。", "数字、时间与主体请以原文为准。"],
        "market_impact": "建议结合后续官方披露与市场数据评估影响。",
        "keywords": [],
        "entities": [],
        "sentiment": "",
        "_source": "fallback",
        "_note": "本地摘录模式（未配置或未调用主备模型）。网页底部可配置 PRIMARY_/FALLBACK_ 环境变量以启用 AI 摘要。",
    }


def call_llm(prompt: str, article: Optional[dict] = None) -> dict:
    models = [get_model("PRIMARY"), get_model("FALLBACK")]
    for cfg in models:
        if not cfg:
            continue
        for _ in range(2):
            try:
                payload = {
                    "model": cfg.model,
                    "temperature": 0.2,
                    "max_tokens": 1600,
                    "messages": [{"role": "user", "content": prompt}],
                }
                headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}
                with httpx.Client(timeout=60) as client:
                    resp = client.post(normalize_url(cfg.base_url), headers=headers, json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                if content.startswith("```"):
                    content = content.strip("`").replace("json", "", 1).strip()
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    parsed["_source"] = "llm"
                    parsed["sentiment"] = str(parsed.get("sentiment", "neutral")).strip().lower()
                return parsed
            except Exception:
                pass
    return _fallback_from_article(article or {})


def _strip_legacy_fallback_noise(text: str) -> str:
    if not text:
        return ""
    lines = [ln for ln in text.splitlines() if "当前无可用模型" not in ln and "建议配置主备模型" not in ln]
    return "\n".join(lines).strip()


def _sentiment_display_html(summ: dict) -> str:
    src = summ.get("_source", "llm")
    if src == "fallback":
        return (
            '<p class="meta-line sentiment-note"><span class="lbl">情绪 Sentiment</span> '
            "<strong>未判定</strong> · 当前为正文摘录模式，未调用语言模型，不做偏多/偏空判断。</p>"
        )
    raw = str(summ.get("sentiment", "neutral")).strip().lower()
    zh = {"positive": "偏多 · Positive bias", "neutral": "中性 · Neutral", "negative": "偏空 · Negative bias"}.get(
        raw, f"中性 · {html.escape(raw)}"
    )
    safe = html.escape(raw)
    return (
        f'<p class="meta-line"><span class="lbl">情绪 Sentiment</span> '
        f'<span class="sent-badge" data-sent="{safe}">{html.escape(zh)}</span></p>'
    )


def _with_rss_note(article: dict, summ: dict) -> dict:
    if article.get("body_source") != "rss":
        return summ
    extra = "正文素材来自 RSS/第三方订阅源中的摘要或片段，可能与 cn.wsj.com 官网全文不一致。"
    prev = str(summ.get("_note") or "").strip()
    summ["_note"] = (prev + " " if prev else "") + extra
    return summ


def summarize(article: dict) -> dict:
    prompt = f"""你是一名财经新闻分析师。请输出 JSON:
{{
  "ai_summary":"100-220字",
  "key_points":["3-5条"],
  "risks":["1-3条"],
  "market_impact":"1段",
  "keywords":["5-8个词"],
  "entities":["3-6个实体"],
  "sentiment":"positive|neutral|negative"
}}
标题: {article['title']}
导语: {article['lede']}
链接: {article['url']}
正文:
{article['body']}
"""
    out = call_llm(prompt, article)
    return _with_rss_note(article, out)


def download_image(url: str, idx: int, run_id: str, cookies: Dict[str, str]) -> Optional[str]:
    if not url:
        return None
    assets_dir = RUNS / run_id / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    ext = ".png" if ".png" in url.lower() else ".jpg"
    name = f"cover_{idx:03d}{ext}"
    path = assets_dir / name
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        with httpx.Client(timeout=20, follow_redirects=True) as c:
            r = c.get(url, headers=headers, cookies=cookies or None)
            r.raise_for_status()
            path.write_bytes(r.content)
        return f"./assets/{name}"
    except Exception:
        return None


def render(records: List[dict], run_id: str) -> Path:
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    cards = []
    for i, r in enumerate(records, start=1):
        summ = r.get("summary") or {}
        pts = [str(x).strip() for x in summ.get("key_points", []) if str(x).strip()][:5]
        rsk = [str(x).strip() for x in summ.get("risks", []) if str(x).strip()][:3]
        points = "".join([f"<li>{html.escape(x)}</li>" for x in pts])
        risks = "".join([f"<li>{html.escape(x)}</li>" for x in rsk])
        safe_title = html.escape(r["title"])
        safe_lede = html.escape(r["lede"])
        safe_pub = html.escape(r.get("published", "") or "—")
        safe_url = html.escape(r["url"], quote=True)
        img_src = html.escape(r["image_local"]) if r.get("image_local") else ""
        og_img = html.escape(r.get("image_url") or "", quote=True)
        if r.get("image_local"):
            hero_block = f"""<figure class="hero-wrap">
  <img class="hero" src="{img_src}" alt="" loading="lazy"/>
  <figcaption class="fig-cap">主配图 · Hero <span class="mono">{og_img}</span></figcaption>
</figure>"""
        else:
            hero_block = """<figure class="hero-wrap hero-missing">
  <div class="hero-placeholder">暂无主配图（站点未返回 og:image 或下载失败）</div>
</figure>"""

        kws = summ.get("keywords") or []
        ents = summ.get("entities") or []
        kw_line = (
            f'<p class="meta-line"><span class="lbl">关键词 Keywords</span> {html.escape(", ".join(str(x) for x in kws))}</p>'
            if kws
            else ""
        )
        ent_line = (
            f'<p class="meta-line"><span class="lbl">实体 Entities</span> {html.escape(", ".join(str(x) for x in ents))}</p>'
            if ents
            else ""
        )
        brief_text = _strip_legacy_fallback_noise(str(summ.get("ai_summary", "")))
        note_html = ""
        if summ.get("_note"):
            note_html = f'<p class="footnote">{html.escape(str(summ["_note"]))}</p>'
        sent_row = _sentiment_display_html(summ)
        cards.append(
            f"""
<article class="story" id="article-{i}">
  <div class="story-rail">
    <span class="index">{i:02d}</span>
  </div>
  <div class="story-body">
    <p class="section-label">原始标题 · Headline</p>
    <h2 class="headline">{safe_title}</h2>
    <p class="meta pub">发布时间（北京时间）· Published <time>{safe_pub}</time></p>
    <p class="section-label">导语 · Dek</p>
    <blockquote class="dek">{safe_lede}</blockquote>
    <p class="section-label">主配图 · Lead art</p>
    {hero_block}
    <div class="ai-block">
      <p class="section-label">AI 摘要 · Brief</p>
      <p class="ai-summary">{html.escape(brief_text)}</p>
      {note_html}
      <p class="section-label">要点 · Key points</p>
      <ul class="list-points">{points or "<li>—</li>"}</ul>
      <p class="section-label">市场影响 · Market context</p>
      <p class="body-text">{html.escape(str(summ.get("market_impact", "")))}</p>
      <p class="section-label">风险与不确定性 · Risks</p>
      <ul class="list-risks">{risks or "<li>—</li>"}</ul>
      {kw_line}
      {ent_line}
      {sent_row}
      <p class="source-line"><a href="{safe_url}" target="_blank" rel="noopener noreferrer">在 cn.wsj.com 打开原文 Open source</a></p>
    </div>
  </div>
</article>
"""
        )

    today = now_bj().strftime("%Y-%m-%d")
    safe_run = html.escape(run_id)
    html_doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>CN WSJ Daily Briefing · {today}</title>
<style>
:root {{
  --ink:#111; --paper:#f7f4ee; --card:#fff; --rule:#d4cfc4; --muted:#5c5c5c;
  --accent:#0a4a8a; --rail:#8b2332;
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; background:var(--paper); color:var(--ink);
  font-family: Georgia, "Times New Roman", "Songti SC", "SimSun", serif;
  font-size:18px; line-height:1.65;
}}
.wrap {{
  max-width:720px; margin:0 auto; padding:32px 20px 56px;
}}
.masthead {{
  border-bottom:3px double var(--ink); padding-bottom:12px; margin-bottom:28px;
}}
.masthead h1 {{
  font-size:clamp(1.75rem, 4vw, 2.35rem); font-weight:700; letter-spacing:0.02em;
  margin:0 0 6px; line-height:1.15;
}}
.masthead .tagline {{
  font-family: Arial, Helvetica, sans-serif; font-size:13px; color:var(--muted);
  letter-spacing:0.04em; text-transform:uppercase;
}}
.masthead .run-id {{
  font-family: ui-monospace, Menlo, monospace; font-size:12px; color:var(--muted); margin-top:6px;
}}
.story {{
  display:flex; gap:16px; align-items:flex-start;
  padding:28px 0; border-bottom:1px solid var(--rule);
}}
.story:last-of-type {{ border-bottom:none; }}
.story-rail {{
  flex:0 0 36px; text-align:right; padding-top:4px;
}}
.story-rail .index {{
  font-family: Arial, sans-serif; font-size:12px; font-weight:700; color:var(--rail);
  letter-spacing:0.06em;
}}
.story-body {{ flex:1; min-width:0; }}
.section-label {{
  font-family: Arial, Helvetica, sans-serif; font-size:11px; letter-spacing:0.12em;
  text-transform:uppercase; color:var(--muted); margin:22px 0 8px;
}}
.section-label:first-of-type {{ margin-top:0; }}
.headline {{
  font-size:clamp(1.45rem, 3.2vw, 1.85rem); line-height:1.25; font-weight:700; margin:0 0 10px;
}}
.meta, .meta-line, .pub {{
  font-family: Arial, Helvetica, sans-serif; font-size:13px; color:var(--muted);
}}
.pub time {{ font-variant-numeric: tabular-nums; }}
.dek {{
  margin:0; padding:12px 0 12px 16px; border-left:4px solid var(--ink);
  font-size:1.08rem; line-height:1.6; color:#222;
}}
.hero-wrap {{ margin:0 0 8px; }}
.hero {{
  display:block; width:100%; max-height:420px; object-fit:cover;
  border:1px solid var(--rule); background:var(--card);
}}
.hero-missing .hero-placeholder {{
  border:1px dashed var(--rule); padding:28px 16px; text-align:center;
  font-family: Arial, sans-serif; font-size:14px; color:var(--muted); background:var(--card);
}}
.fig-cap {{
  font-family: Arial, sans-serif; font-size:11px; color:var(--muted); margin-top:6px; line-height:1.4;
}}
.fig-cap .mono {{ word-break:break-all; font-family: ui-monospace, Menlo, monospace; }}
.ai-block {{ margin-top:8px; }}
.ai-summary, .body-text {{
  margin:0 0 12px; hyphens:auto;
}}
.list-points, .list-risks {{
  margin:0 0 12px; padding-left:1.2em;
}}
.list-points li {{ margin-bottom:6px; }}
.list-risks li {{ margin-bottom:6px; color:#333; }}
.meta-line .lbl {{ font-weight:600; margin-right:6px; }}
.footnote {{
  font-family: Arial, sans-serif; font-size:12px; color: var(--muted); margin: -4px 0 14px;
  line-height:1.45; padding-left: 2px;
}}
.sentiment-note {{ margin-top: 10px; }}
.sentiment-note strong {{ font-weight: 600; }}
.sent-badge {{
  display:inline-block; font-family: Arial, sans-serif; font-size:12px;
  padding:2px 8px; border:1px solid var(--rule); border-radius:2px; text-transform:capitalize;
}}
.source-line {{
  margin-top:18px; font-family: Arial, sans-serif; font-size:14px;
}}
a {{ color:var(--accent); text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
@media print {{
  body {{ background:#fff; }}
  .story {{ break-inside:avoid; }}
}}
.empty {{
  font-family: Arial, sans-serif; font-size:15px; color: var(--muted); padding: 24px 0;
}}
</style>
</head>
<body>
  <main class="wrap">
    <header class="masthead">
      <h1>The Wall Street Journal</h1>
      <p class="tagline">China edition briefing · cn.wsj.com</p>
      <p class="run-id">Run · {safe_run} · 生成时间 Generated {html.escape(now_bj().strftime("%Y-%m-%d %H:%M:%S"))} · 时间窗 {brief_window_hours()}h（北京时间）</p>
    </header>
    {''.join(cards) if cards else '<p class="empty">本版暂无符合条件的文章（时间窗口内无可用正文或全部被站点拦截）。</p>'}
  </main>
</body>
</html>"""
    out = run_dir / "index.html"
    out.write_text(html_doc, encoding="utf-8")
    return out


def _run_sort_key(path: Path) -> datetime:
    try:
        return datetime.strptime(path.parent.name, "%Y-%m-%d_%H-%M-%S").replace(tzinfo=BJ_TZ)
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=BJ_TZ)


def render_portal(run_id: str, records_count: int) -> Path:
    DOCS.mkdir(parents=True, exist_ok=True)
    links = []
    run_pages = sorted(RUNS.glob("*/index.html"), key=_run_sort_key, reverse=True)
    for p in run_pages:
        rid = p.parent.name
        links.append(f'<li><a href="./runs/{html.escape(rid)}/">{html.escape(rid)}</a></li>')
        if len(links) >= 20:
            break
    portal_html = f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>CN WSJ Daily Briefing Portal</title></head>
<body style="font-family:Arial,sans-serif;max-width:860px;margin:24px auto;padding:0 12px;">
  <h1>CN WSJ Daily Briefing</h1>
  <p>最新版本：<a href="./runs/{html.escape(run_id)}/">{html.escape(run_id)}</a>（{records_count} 篇）</p>
  <h3>历史版本（最近20次）</h3>
  <ul>{''.join(links)}</ul>
</body></html>"""
    portal = DOCS / "index.html"
    portal.write_text(portal_html, encoding="utf-8")
    return portal


def run() -> int:
    run_id = now_bj().strftime("%Y-%m-%d_%H-%M-%S")
    FETCH_HOME_META.clear()
    network_preflight(run_id)
    cookies = load_netscape_cookies(run_id)
    raw_cookie = os.getenv("COOKIE_FILE", "").strip()
    if raw_cookie:
        if not cookies:
            print("警告：COOKIE_FILE 已指定但 Cookie 为空或路径无效。")
    else:
        print("提示：未设置 COOKIE_FILE；站点正文抓取将依赖 PLAYWRIGHT_STORAGE_STATE（若配置了 RSS 则可能仅用 RSS 文本）。")

    discovery = os.getenv("ARTICLE_DISCOVERY", "both").strip().lower()
    if discovery not in ("homepage", "rss", "both"):
        discovery = "both"

    rss_feed_urls = rss_env_feed_urls()
    rss_hints: Dict[str, dict] = {}
    rss_order: List[str] = []

    if discovery in ("rss", "both"):
        if not rss_feed_urls:
            print("提示：未发现 WSJ_RSS_URLS/RSS_URLS（建议在 DataDome 拦截时填写公开或自建 RSS，逗号分隔多个地址）。")
        else:
            rss_hints = rss_discover(rss_feed_urls, run_id)
            rss_order = list(rss_hints.keys())

    home_links: List[str] = []
    if discovery in ("homepage", "both"):
        home_links = article_links(cookies, run_id)

    ordered: List[str] = []
    seen_merge: set = set()
    for u in rss_order:
        if u not in seen_merge:
            seen_merge.add(u)
            ordered.append(u)
    for h in home_links:
        c = canonical_wsj_article_url(h)
        if c not in seen_merge:
            seen_merge.add(c)
            ordered.append(c)

    now = now_bj()
    window_h = brief_window_hours()
    allow_undated = allow_undated_articles()
    undated_cap = max_undated_articles()
    undated_kept = 0
    print(
        f"[DEBUG] discovery={discovery!r} rss_feeds={len(rss_feed_urls)} rss_items={len(rss_hints)} "
        f"homepage_links={len(home_links)} merged={len(ordered)} window_h={window_h} "
        f"allow_undated={allow_undated} undated_cap={undated_cap}"
    )

    if discovery == "rss" and not rss_feed_urls:
        print("[ERROR] ARTICLE_DISCOVERY=rss 但未配置 WSJ_RSS_URLS（或 RSS_URLS）。")
        return 2
    home_snip = FETCH_HOME_META.get("http_401_body", "")
    if discovery in ("homepage", "both") and len(home_links) == 0 and (
        "captcha-delivery" in home_snip.lower() or "geo.captcha-delivery" in home_snip.lower()
    ):
        msg = (
            "首页返回反爬/验证码页（DataDome，见日志 H6）。可改用 RSS：设置 WSJ_RSS_URLS 并将 ARTICLE_DISCOVERY=rss|both；"
            "或在本机用有界面浏览器登录后执行 scripts/save_cn_wsj_storage_state.py，并设置 PLAYWRIGHT_STORAGE_STATE。"
        )
        print(f"[BLOCKED] {msg}")
        # #region agent log
        _dbg(
            run_id,
            "H9",
            "run.py:run",
            "homepage blocked by bot challenge",
            {"hint": "datadome", "has_storage_state": bool(os.getenv("PLAYWRIGHT_STORAGE_STATE", "").strip()), "rss_items": len(rss_hints)},
        )
        # #endregion

    seen_urls: set = set()
    records = []
    parsed_articles: List[dict] = []
    parsed_ok = 0
    missing_time = 0
    out_of_window = 0
    idx = 0
    for link in ordered:
        if link in seen_urls:
            continue
        rss_hint = rss_hints.get(link)
        art = parse_article(link, cookies, run_id)
        if art and rss_hint and not art.get("published_bj") and rss_hint.get("published_bj"):
            art["published_bj"] = rss_hint["published_bj"]
        elif not art and rss_hint:
            art = article_from_rss_hint(link, rss_hint, run_id)
        if not art:
            continue
        parsed_ok += 1
        parsed_articles.append(art)
        if not in_brief_window(art.get("published_bj"), now, window_h):
            if not art.get("published_bj"):
                missing_time += 1
                if allow_undated and undated_kept < undated_cap:
                    undated_kept += 1
                    art["_undated_included"] = True
                else:
                    continue
            else:
                out_of_window += 1
                continue
        seen_urls.add(link)
        idx += 1
        s = summarize(art)
        if art.get("_undated_included"):
            note = str(s.get("_note") or "").strip()
            extra = f"发布时间缺失，按无时间兜底策略纳入（最多 {undated_cap} 篇）。"
            s["_note"] = (note + " " + extra).strip() if note else extra
        local_img = download_image(art.get("image_url", ""), idx, run_id, cookies)
        records.append(
            {
                "url": art["url"],
                "title": art["title"],
                "lede": art["lede"],
                "published": art["published_bj"].strftime("%Y-%m-%d %H:%M:%S") if art.get("published_bj") else "",
                "image_url": art.get("image_url") or "",
                "image_local": local_img,
                "summary": s,
            }
        )
    allow_stale = os.getenv("ALLOW_STALE_FALLBACK", "1").strip().lower() in ("1", "true", "yes", "on")
    fallback_target = int(os.getenv("STALE_FALLBACK_LIMIT", "8").strip() or "8")
    fallback_target = max(1, min(30, fallback_target))
    if allow_stale and not records and parsed_articles:
        print(
            f"[FALLBACK] no records in {window_h}h window; include up to {fallback_target} parsed articles for continuity"
        )
        # #region agent log
        _dbg(
            run_id,
            "H15",
            "run.py:run",
            "stale fallback activated",
            {"parsed_ok": parsed_ok, "window_h": window_h, "fallback_target": fallback_target},
        )
        # #endregion
        fallback_pool = sorted(
            parsed_articles,
            key=lambda a: (a.get("published_bj") is not None, a.get("published_bj") or datetime(1970, 1, 1, tzinfo=BJ_TZ)),
            reverse=True,
        )[:fallback_target]
        for art in fallback_pool:
            if art["url"] in seen_urls:
                continue
            seen_urls.add(art["url"])
            idx += 1
            s = summarize(art)
            local_img = download_image(art.get("image_url", ""), idx, run_id, cookies)
            if s.get("_source") == "fallback":
                note = str(s.get("_note") or "").strip()
                extra = f"本次未命中最近 {window_h} 小时时间窗，已回退展示最近可解析文章。"
                s["_note"] = (note + " " + extra).strip() if note else extra
            records.append(
                {
                    "url": art["url"],
                    "title": art["title"],
                    "lede": art["lede"],
                    "published": art["published_bj"].strftime("%Y-%m-%d %H:%M:%S") if art.get("published_bj") else "",
                    "image_url": art.get("image_url") or "",
                    "image_local": local_img,
                    "summary": s,
                }
            )
    run_page = render(records, run_id)
    render_portal(run_id, len(records))
    print(
        f"[DEBUG] parsed_ok={parsed_ok}, missing_time={missing_time}, out_of_window={out_of_window}, final_records={len(records)}"
    )
    # #region agent log
    _dbg(
        run_id,
        "H4",
        "run.py:run",
        "run summary",
        {
            "homepage_links": len(home_links),
            "rss_items": len(rss_hints),
            "merged_links": len(ordered),
            "parsed_ok": parsed_ok,
            "missing_time": missing_time,
            "out_of_window": out_of_window,
            "undated_kept": undated_kept,
            "final_records": len(records),
        },
    )
    # #endregion
    print(f"Generated run page: {run_page}")
    print(f"Latest portal: {DOCS / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
