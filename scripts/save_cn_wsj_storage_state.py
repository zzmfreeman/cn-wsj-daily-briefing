#!/usr/bin/env python3
"""One-time: open a real Chrome window, log in to cn.wsj.com, then save Playwright storage_state.

无需在终端按回车：用 --wait-seconds 在登录完成后自动写入（默认 120 秒）。"""
import argparse
import asyncio
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "cn_wsj_storage.json"


async def main_async(out: Path, wait_seconds: int) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chrome", headless=False)
        except Exception:
            browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto("https://cn.wsj.com/", wait_until="domcontentloaded", timeout=60000)
        print(
            f"请在浏览器中完成登录/验证；{wait_seconds}s 后自动写入 Playwright 状态：\n{out}\n",
            flush=True,
        )
        await asyncio.sleep(max(5, int(wait_seconds)))
        out.parent.mkdir(parents=True, exist_ok=True)
        await ctx.storage_state(path=str(out))
        await browser.close()
    print(f"已保存：{out}\n请设置：export PLAYWRIGHT_STORAGE_STATE=\"{out}\"")


def main() -> None:
    ap = argparse.ArgumentParser(description="Save cn.wsj.com session for Playwright reuse.")
    ap.add_argument("--out", type=Path, default=OUT, help="Output JSON path (Playwright storage_state).")
    ap.add_argument("--wait-seconds", type=int, default=120, help="Seconds to wait after opening the page before saving.")
    args = ap.parse_args()
    out = args.out.expanduser().resolve()
    asyncio.run(main_async(out, args.wait_seconds))


if __name__ == "__main__":
    main()
