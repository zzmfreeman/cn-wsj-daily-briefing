#!/usr/bin/env python3
import asyncio
import html
import json
import os
import re
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
RUNS = DOCS / "runs"


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


def _fallback_from_article(article: dict) -> dict:
    body = (article.get("body") or "").strip()
    chunks = [s.strip() for s in re.split(r"[。！？\n]+", body) if len(s.strip()) > 20]
    key_points = chunks[:5] if chunks else ["正文过短，无法提炼要点。"]
    lead = (article.get("lede") or "").strip()
    ai_summary = (
        (lead + " ")[:80] + (chunks[0] if chunks else "请阅读原文。")
    )[:220]
    if len(ai_summary) < 40 and chunks:
        ai_summary = chunks[0][:220]
    return {
        "ai_summary": ai_summary,
        "key_points": key_points,
        "risks": ["未调用远程模型，以下为基于正文的机械摘录。", "数字、时间与主体请以原文为准。"],
        "market_impact": "需结合后续披露与市场数据评估影响方向。",
        "keywords": [],
        "entities": [],
        "sentiment": "neutral",
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
                return json.loads(content)
            except Exception:
                pass
    return _fallback_from_article(article or {})


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
    return call_llm(prompt, article)


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
        sent = html.escape(str(summ.get("sentiment", "neutral")))
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
      <p class="ai-summary">{html.escape(str(summ.get("ai_summary", "")))}</p>
      <p class="section-label">要点 · Key points</p>
      <ul class="list-points">{points or "<li>—</li>"}</ul>
      <p class="section-label">市场影响 · Market context</p>
      <p class="body-text">{html.escape(str(summ.get("market_impact", "")))}</p>
      <p class="section-label">风险与不确定性 · Risks</p>
      <ul class="list-risks">{risks or "<li>—</li>"}</ul>
      {kw_line}
      {ent_line}
      <p class="meta-line"><span class="lbl">情绪 Sentiment</span> <span class="sent-badge" data-sent="{sent}">{sent}</span></p>
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
      <p class="run-id">Run · {safe_run} · 生成时间 Generated {html.escape(now_bj().strftime("%Y-%m-%d %H:%M:%S"))} · 过去24小时（北京时间）</p>
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
    html = f"""<!doctype html>
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
    portal.write_text(html, encoding="utf-8")
    return portal


def run() -> int:
    run_id = now_bj().strftime("%Y-%m-%d_%H-%M-%S")
    cookies = load_netscape_cookies()
    if not cookies:
        print("警告：未读取到 COOKIE_FILE，cn.wsj.com 可能返回 401。")
    now = now_bj()
    links = article_links(cookies)
    seen_urls: set = set()
    records = []
    idx = 0
    for link in links:
        if link in seen_urls:
            continue
        art = parse_article(link, cookies)
        if not art:
            continue
        if not in_last_24h(art.get("published_bj"), now):
            continue
        seen_urls.add(link)
        idx += 1
        s = summarize(art)
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
    run_page = render(records, run_id)
    render_portal(run_id, len(records))
    print(f"Generated run page: {run_page}")
    print(f"Latest portal: {DOCS / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
