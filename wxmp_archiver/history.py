"""Crawl WeChat Official Account history page to collect article links.

Two strategies (auto-selected):

1. **Platform API** (default) — Open mp.weixin.qq.com, user scans QR code to
   login, then use the internal ``appmsg`` API to enumerate every article from
   the target account.  Requires a WeChat Official Account (personal
   subscription accounts work and are free to register).
2. **Profile ext** (fallback when ``--cookie`` is supplied) — Open the
   ``profile_ext`` page with injected cookies and intercept ``getmsg`` JSON
   responses + DOM links.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Page, Response, async_playwright

from . import storage

logger = logging.getLogger(__name__)

_WECHAT_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Mobile/15E148 MicroMessenger/8.0.47(0x18002f2c) "
    "NetType/WIFI Language/zh_CN"
)

# ---------------------------------------------------------------------------
# URL / biz helpers
# ---------------------------------------------------------------------------

def _extract_biz(history_url: str) -> str:
    parsed = urlparse(history_url)
    params = parse_qs(parsed.query)
    biz = params.get("__biz", [""])[0]
    if not biz:
        raise ValueError(f"Cannot extract __biz from URL: {history_url}")
    return biz


def _biz_to_fakeid(biz: str) -> str:
    """Ensure __biz has proper base64 padding for use as fakeid."""
    remainder = len(biz) % 4
    if remainder:
        biz += "=" * (4 - remainder)
    return biz


def _normalize_wx_url(url: str) -> str | None:
    if not url:
        return None
    url = url.replace("&amp;", "&")
    if url.startswith("http://"):
        url = url.replace("http://", "https://", 1)
    if "mp.weixin.qq.com/s" not in url:
        return None
    return url.split("#")[0]


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

async def crawl_links(
    history_url: str,
    out_dir: str | Path,
    max_pages: int = 999,
    headless: bool = False,
    max_empty_rounds: int = 3,
    cookies: list[dict] | None = None,
) -> list[dict]:
    out_dir = Path(out_dir)
    links_path = out_dir / "links.jsonl"

    existing = storage.read_jsonl(links_path)
    seen_urls: set[str] = {r["url"] for r in existing}
    all_links: list[dict] = list(existing)

    def _merge_and_save(batch: list[dict]) -> int:
        """Merge a batch of new links, deduplicate, and flush to disk.
        Returns number of new links added."""
        added = 0
        for item in batch:
            norm = _normalize_wx_url(item["url"])
            if norm and norm not in seen_urls:
                seen_urls.add(norm)
                item["url"] = norm
                all_links.append(item)
                added += 1
        if added:
            all_links.sort(key=lambda x: x.get("publish_time") or "")
            storage.write_jsonl(links_path, all_links)
        return added

    if cookies:
        logger.info("Using profile_ext strategy (cookies provided)")
        new_links = await _crawl_via_profile_ext(
            history_url, headless, max_pages, max_empty_rounds, cookies
        )
        _merge_and_save(new_links)
    else:
        biz = _extract_biz(history_url)
        logger.info("Using platform QR-login strategy (__biz=%s)", biz)
        await _crawl_via_platform(biz, max_pages, on_batch=_merge_and_save)

    logger.info("Total %d links saved to %s", len(all_links), links_path)
    return all_links


# ===================================================================
# Strategy 1: QR login to mp.weixin.qq.com + platform API
# ===================================================================

async def _crawl_via_platform(
    biz: str,
    max_pages: int,
    on_batch: Callable[[list[dict]], int] | None = None,
) -> None:
    fakeid = _biz_to_fakeid(biz)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()

        token = await _platform_login(page)
        await _platform_list_articles(page, fakeid, token, max_pages, on_batch=on_batch)

        await browser.close()


async def _platform_login(page: Page) -> str:
    """Navigate to mp.weixin.qq.com and wait for the user to scan QR code."""
    logger.info(
        "Opening mp.weixin.qq.com login page…\n"
        "  → Please scan the QR code with WeChat on your phone.\n"
        "  → Then confirm login on your phone."
    )
    await page.goto("https://mp.weixin.qq.com/", wait_until="networkidle", timeout=30_000)

    # Wait up to 3 minutes for the user to complete QR login.
    # After successful login the URL contains ``token=<digits>``.
    try:
        await page.wait_for_url(re.compile(r"token=\d+"), timeout=180_000)
    except Exception:
        # Check if we ended up on an error page (no OA bound)
        body_text = await page.inner_text("body")
        if "绑定" in body_text or "注册" in body_text:
            raise RuntimeError(
                "Login failed — your WeChat account does not have a linked "
                "Official Account (公众号). You need at least a free personal "
                "subscription account (个人订阅号) to use QR login.\n"
                "Alternative: use --cookie to provide cookies from mitmproxy."
            )
        raise RuntimeError(
            "Login timed out (180s). Please scan the QR code promptly and "
            "confirm on your phone."
        )

    match = re.search(r"token=(\d+)", page.url)
    if not match:
        raise RuntimeError("Failed to extract token from URL after login.")

    logger.info("Login successful.")
    return match.group(1)


async def _platform_list_articles(
    page: Page,
    fakeid: str,
    token: str,
    max_pages: int,
    on_batch: Callable[[list[dict]], int] | None = None,
) -> list[dict]:
    """Paginate through the mp.weixin.qq.com ``appmsg`` API.

    After each successful page, *on_batch* is called with the new articles
    so the caller can flush them to disk immediately.
    """
    articles: list[dict] = []
    begin = 0
    count = 5
    total: int | None = None
    page_num = 0
    consecutive_rate_limits = 0
    max_rate_limit_retries = 3

    while page_num < max_pages:
        api_url = (
            "https://mp.weixin.qq.com/cgi-bin/appmsg"
            f"?action=list_ex&begin={begin}&count={count}"
            f"&fakeid={fakeid}&type=9&query="
            f"&token={token}&lang=zh_CN&f=json&ajax=1"
        )

        try:
            data = await page.evaluate(
                """async (url) => {
                    const r = await fetch(url, {
                        credentials: 'include',
                        headers: {'X-Requested-With': 'XMLHttpRequest'}
                    });
                    return await r.json();
                }""",
                api_url,
            )
        except Exception as exc:
            logger.error("Platform API call failed: %s", exc)
            break

        ret = data.get("base_resp", {}).get("ret")

        if ret == 200013:
            consecutive_rate_limits += 1
            if consecutive_rate_limits >= max_rate_limit_retries:
                logger.warning(
                    "Rate-limited %d times in a row — saving %d articles collected so far.",
                    consecutive_rate_limits,
                    len(articles),
                )
                break
            wait = 60 * consecutive_rate_limits
            logger.warning(
                "Rate-limited by platform API (attempt %d/%d) — waiting %ds…",
                consecutive_rate_limits,
                max_rate_limit_retries,
                wait,
            )
            await asyncio.sleep(wait)
            continue

        consecutive_rate_limits = 0

        if ret == 200003:
            logger.error("Token expired. Please re-run and login again.")
            break

        if ret != 0:
            err = data.get("base_resp", {}).get("err_msg", "unknown")
            logger.error("Platform API error ret=%s: %s", ret, err)
            break

        if total is None:
            total = data.get("app_msg_cnt", 0)
            logger.info("Target account has %d articles", total)

        msg_list = data.get("app_msg_list", [])
        if not msg_list:
            break

        batch: list[dict] = []
        for item in msg_list:
            link = item.get("link", "")
            if not link:
                continue
            ct = item.get("create_time")
            pub_time = datetime.fromtimestamp(ct).isoformat() if ct else None
            batch.append(
                {
                    "url": link,
                    "title": item.get("title", ""),
                    "publish_time": pub_time,
                    "source": "platform_api",
                }
            )

        articles.extend(batch)

        if on_batch and batch:
            saved = on_batch(batch)
            logger.info(
                "Page %d: +%d new links saved to disk (%d total collected)",
                page_num + 1,
                saved,
                len(articles),
            )
        else:
            logger.info("Page %d: %d articles collected", page_num + 1, len(articles))

        page_num += 1
        begin += count
        if total is not None and begin >= total:
            break

        # Conservative delay to avoid rate-limiting (8–15s between API calls)
        await asyncio.sleep(random.uniform(8, 15))

    return articles


# ===================================================================
# Strategy 2: profile_ext with injected cookies (advanced / fallback)
# ===================================================================

async def _crawl_via_profile_ext(
    history_url: str,
    headless: bool,
    max_pages: int,
    max_empty_rounds: int,
    cookies: list[dict],
) -> list[dict]:
    network_items: list[dict] = []
    can_continue = True
    api_error_flag = False

    async def _on_response(response: Response) -> None:
        nonlocal can_continue, api_error_flag
        url = response.url
        if "mp.weixin.qq.com/mp/profile_ext" not in url:
            return
        if "action=getmsg" not in url:
            return
        try:
            body = await response.body()
            data = json.loads(body)
            ret = data.get("ret")
            if ret != 0:
                logger.warning("API ret=%s errmsg=%s", ret, data.get("errmsg", ""))
                api_error_flag = True
                return
            can_continue = bool(data.get("can_msg_continue", 0))
            msg_list_str = data.get("general_msg_list", "")
            if not msg_list_str:
                return
            msg_list = json.loads(msg_list_str)
            for item in msg_list.get("list", []):
                comm = item.get("comm_msg_info", {})
                ext = item.get("app_msg_ext_info", {})
                ts = comm.get("datetime")
                pub_time = datetime.fromtimestamp(ts).isoformat() if ts else None
                entries: list[dict] = []
                if ext.get("content_url"):
                    entries.append(ext)
                for sub in ext.get("multi_app_msg_item_list", []):
                    if sub.get("content_url"):
                        entries.append(sub)
                for entry in entries:
                    raw = entry["content_url"].replace("&amp;", "&")
                    if raw.startswith("http://"):
                        raw = raw.replace("http://", "https://", 1)
                    network_items.append(
                        {
                            "url": raw,
                            "title": entry.get("title", ""),
                            "publish_time": pub_time,
                            "source": "network",
                        }
                    )
        except Exception as exc:
            logger.debug("Failed to parse profile_ext response: %s", exc)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(
            user_agent=_WECHAT_UA,
            viewport={"width": 375, "height": 812},
        )
        if cookies:
            await ctx.add_cookies(cookies)
            logger.info("Injected %d cookies", len(cookies))
        page = await ctx.new_page()
        page.on("response", _on_response)

        logger.info("Opening history page: %s", history_url)
        try:
            await page.goto(history_url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            logger.error("Failed to open history page: %s", exc)
            await browser.close()
            raise

        await asyncio.sleep(3)

        if await _check_blocked(page):
            if headless:
                logger.error(
                    "Detected login wall. Re-run without --cookie to use QR login, "
                    "or re-run with --headless false."
                )
                await browser.close()
                raise SystemExit(1)
            else:
                await _wait_for_user_verification(page)

        empty_rounds = 0
        prev_count = 0
        round_num = 0

        while round_num < max_pages and empty_rounds < max_empty_rounds and can_continue:
            if api_error_flag:
                logger.error("API error, stopping.")
                break
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1.5)
            for selector in (
                ".js_profile_load_more",
                "#js_profile_load_more",
                "text=加载更多",
            ):
                btn = await page.query_selector(selector)
                if btn and await btn.is_visible():
                    try:
                        await btn.click()
                        await asyncio.sleep(2)
                    except Exception:
                        pass
                    break
            await asyncio.sleep(1)
            cur = len(network_items)
            if cur == prev_count:
                empty_rounds += 1
                logger.info("No new articles (round %d/%d)", empty_rounds, max_empty_rounds)
            else:
                empty_rounds = 0
                round_num += 1
                logger.info("Collected %d links from network", cur)
            prev_count = cur

        dom_links = await _extract_dom_links(page)
        await browser.close()

    return network_items + dom_links


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def _extract_dom_links(page: Page) -> list[dict]:
    links: list[dict] = []
    elements = await page.query_selector_all('a[href*="mp.weixin.qq.com/s"]')
    for el in elements:
        href = await el.get_attribute("href")
        if not href:
            continue
        title = ""
        try:
            title = (await el.inner_text()).strip()
        except Exception:
            pass
        links.append(
            {
                "url": href.replace("&amp;", "&"),
                "title": title,
                "publish_time": None,
                "source": "dom",
            }
        )
    return links


async def _check_blocked(page: Page) -> bool:
    for selector in (
        "text=请在微信客户端打开",
        "text=环境异常",
        "text=操作频繁",
        ".weui-msg__title",
    ):
        el = await page.query_selector(selector)
        if el and await el.is_visible():
            return True
    return False


async def _wait_for_user_verification(page: Page, timeout: int = 300) -> None:
    logger.warning(
        "Detected login / verification wall.\n"
        "  → Please complete verification in the browser window.\n"
        "  → Waiting up to %d seconds…",
        timeout,
    )
    elapsed = 0
    interval = 3
    while elapsed < timeout:
        await asyncio.sleep(interval)
        elapsed += interval
        if not await _check_blocked(page):
            logger.info("Verification passed — resuming.")
            return
    logger.error("Timed out waiting for verification (%ds).", timeout)
    raise SystemExit(1)
