#!/usr/bin/env python3
import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import httpx
from bs4 import BeautifulSoup
from dateutil import parser as dt_parser

BJ_TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).parent
DOCS = ROOT / "docs"
ASSETS = DOCS / "assets"
STATE_FILE = ROOT / "state_seen.json"


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


def load_netscape_cookies() -> Dict[str, str]:
    cookie_file = Path(os.getenv("COOKIE_FILE", "")).expanduser()
    jar: Dict[str, str] = {}
    if not cookie_file.exists():
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
            jar[parts[5].strip()] = parts[6].strip()
    return jar


def fetch_html(url: str, cookies: Dict[str, str]) -> str:
    headers = {"User-Agent": "Mozilla/5.0"}
    with httpx.Client(timeout=25, follow_redirects=True) as client:
        r = client.get(url, headers=headers, cookies=cookies)
        if r.status_code in (401, 403):
            return fetch_html_playwright(url, cookies)
        r.raise_for_status()
        return r.text


def fetch_html_playwright(url: str, cookies: Dict[str, str]) -> str:
    async def _go() -> str:
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1366, "height": 900},
            )
            if cookies:
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
            if resp and resp.status >= 400:
                raise RuntimeError(f"playwright status={resp.status}")
            await asyncio.sleep(2)
            html = await page.content()
            await browser.close()
            return html

    return asyncio.run(_go())


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


def in_last_24h(published: Optional[datetime], now: datetime) -> bool:
    if not published:
        return False
    return now - timedelta(hours=24) <= published <= now


def article_links(cookies: Dict[str, str]) -> List[str]:
    try:
        html = fetch_html("https://cn.wsj.com/", cookies)
    except Exception:
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
    return out


def parse_article(url: str, cookies: Dict[str, str]) -> Optional[dict]:
    try:
        html = fetch_html(url, cookies)
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
            return None
        return {
            "url": url,
            "title": title,
            "lede": lede or "（站点未提供明确导语）",
            "image_url": image,
            "published_bj": parse_bj(published) or parse_bj(modified),
            "body": body,
        }
    except Exception:
        return None


def unique_key(article: dict) -> str:
    h = hashlib.sha1(article["title"].strip().lower().encode("utf-8")).hexdigest()[:16]
    return f"{article['url']}::{h}"


def load_seen() -> set:
    if not STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("seen", []))
    except Exception:
        return set()


def save_seen(keys: set) -> None:
    STATE_FILE.write_text(json.dumps({"seen": list(keys)[-5000:]}, ensure_ascii=False, indent=2), encoding="utf-8")


def call_llm(prompt: str) -> dict:
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
                return json.loads(content)
            except Exception:
                pass
    return {
        "ai_summary": "当前无可用模型，使用本地兜底摘要。建议配置主备模型以提升质量。",
        "key_points": [x for x in prompt.split("\n")[:5] if x][:3],
        "risks": ["兜底摘要可能遗漏细节。", "请对关键事实做人工复核。"],
        "market_impact": "短期影响需结合后续市场数据判断。",
        "keywords": ["WSJ", "市场", "风险", "政策", "观察"],
        "entities": ["WSJ", "市场参与者", "监管机构"],
        "sentiment": "neutral",
    }


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
    return call_llm(prompt)


def download_image(url: str, idx: int) -> Optional[str]:
    if not url:
        return None
    ASSETS.mkdir(parents=True, exist_ok=True)
    ext = ".png" if ".png" in url.lower() else ".jpg"
    name = f"cover_{idx:03d}{ext}"
    path = ASSETS / name
    try:
        with httpx.Client(timeout=20, follow_redirects=True) as c:
            r = c.get(url)
            r.raise_for_status()
            path.write_bytes(r.content)
        return f"./assets/{name}"
    except Exception:
        return None


def render(records: List[dict]) -> Path:
    DOCS.mkdir(parents=True, exist_ok=True)
    cards = []
    for r in records:
        points = "".join([f"<li>{x}</li>" for x in r["summary"].get("key_points", [])[:5]])
        risks = "".join([f"<li>{x}</li>" for x in r["summary"].get("risks", [])[:3]])
        img = f"<img class='hero' src='{r['image_local']}' alt='hero image'/>" if r.get("image_local") else ""
        cards.append(
            f"""
            <article class="story">
              {img}
              <h2>{r['title']}</h2>
              <p class="lede">{r['lede']}</p>
              <p class="meta">发布时间（北京时间）：{r.get('published', 'unknown')}</p>
              <p><strong>AI 摘要</strong> {r['summary'].get('ai_summary','')}</p>
              <p><strong>市场影响</strong> {r['summary'].get('market_impact','')}</p>
              <p><strong>关键词</strong> {", ".join(r['summary'].get('keywords', []))}</p>
              <p><strong>实体</strong> {", ".join(r['summary'].get('entities', []))}</p>
              <p><strong>情绪</strong> {r['summary'].get('sentiment', 'neutral')}</p>
              <ul>{points}</ul>
              <ul>{risks}</ul>
              <p><a href="{r['url']}" target="_blank">查看原文</a></p>
            </article>
            """
        )

    today = now_bj().strftime("%Y-%m-%d")
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>CN WSJ Daily Briefing {today}</title>
<style>
body{{margin:0;background:#f4f1ea;color:#111;font-family:Georgia,"Times New Roman",serif;}}
.wrap{{max-width:980px;margin:0 auto;padding:24px 18px 40px;}}
h1{{font-size:40px;letter-spacing:0.5px;margin:8px 0 4px;border-bottom:2px solid #111;padding-bottom:8px;}}
.sub{{font-family:Arial,sans-serif;color:#444;margin-bottom:18px;}}
.story{{background:#fff;border:1px solid #ddd;padding:18px 18px 14px;margin-bottom:18px;box-shadow:0 1px 0 rgba(0,0,0,.03);}}
.hero{{width:100%;max-height:360px;object-fit:cover;margin-bottom:12px;border:1px solid #ddd;}}
h2{{font-size:28px;line-height:1.3;margin:2px 0 8px;}}
.lede{{font-size:18px;line-height:1.6;color:#222;}}
.meta{{font-size:13px;color:#666;font-family:Arial,sans-serif;}}
p,li{{font-size:17px;line-height:1.65;}}
a{{color:#0a4a8a;text-decoration:none;}}
a:hover{{text-decoration:underline;}}
</style>
</head>
<body>
  <main class="wrap">
    <h1>The Wall Street Journal China Briefing</h1>
    <p class="sub">自动抓取 cn.wsj.com | 过去24小时（北京时间）| 生成时间：{now_bj().strftime("%Y-%m-%d %H:%M:%S")}</p>
    {''.join(cards)}
  </main>
</body>
</html>"""
    out = DOCS / "index.html"
    out.write_text(html, encoding="utf-8")
    return out


def run() -> int:
    cookies = load_netscape_cookies()
    now = now_bj()
    links = article_links(cookies)
    seen = load_seen()
    records = []
    for idx, link in enumerate(links, start=1):
        art = parse_article(link, cookies)
        if not art:
            continue
        if not in_last_24h(art.get("published_bj"), now):
            continue
        key = unique_key(art)
        if key in seen:
            continue
        seen.add(key)
        s = summarize(art)
        local_img = download_image(art.get("image_url", ""), idx)
        records.append(
            {
                "url": art["url"],
                "title": art["title"],
                "lede": art["lede"],
                "published": art["published_bj"].strftime("%Y-%m-%d %H:%M:%S") if art.get("published_bj") else "",
                "image_local": local_img,
                "summary": s,
            }
        )
    render(records)
    save_seen(seen)
    print(f"Generated docs/index.html with {len(records)} articles.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
