"""Fetch individual WeChat articles: extract content, download images, save locally."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from markdownify import markdownify as md
from playwright.async_api import Page, Response, async_playwright

from . import assets, storage

logger = logging.getLogger(__name__)

_WECHAT_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Mobile/15E148 MicroMessenger/8.0.47(0x18002f2c) "
    "NetType/WIFI Language/zh_CN"
)


# ---------------------------------------------------------------------------
# Single-article fetch
# ---------------------------------------------------------------------------

async def fetch_article(
    page: Page,
    url: str,
    out_dir: Path,
    aid: str,
    link_meta: dict | None = None,
) -> dict:
    """Open one article URL, extract content, download images, save files.

    Returns the meta dict written to meta.json.
    """
    intercepted_images: dict[str, tuple[bytes, str | None]] = {}

    async def _capture_image(response: Response) -> None:
        resp_url = response.url
        ct = response.headers.get("content-type", "")
        if not ct.startswith("image/"):
            return
        try:
            body = await response.body()
            intercepted_images[resp_url] = (body, ct)
        except Exception:
            pass

    page.on("response", _capture_image)

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    except Exception as exc:
        raise RuntimeError(f"Failed to load {url}: {exc}") from exc

    # Wait for article content to render
    try:
        await page.wait_for_selector("#js_content", state="visible", timeout=15_000)
    except Exception:
        logger.warning("Timeout waiting for #js_content on %s — trying fallback", url)

    await asyncio.sleep(1)

    # Detect blocks / login walls
    if await _check_article_blocked(page):
        raise RuntimeError(
            "Article page shows login/verification wall. "
            "Re-run with --headless false and complete verification, "
            "or reduce request frequency."
        )

    # ------ Extract metadata ------
    title = await _extract_text(page, "#activity-name") or await _extract_text(page, "h1")
    author = await _extract_text(page, "#js_name") or await _extract_text(page, ".rich_media_meta_text")
    account_name = await _extract_text(page, "#js_name") or await _extract_text(page, "#profileBt")
    pub_time = await _extract_text(page, "#publish_time") or await _extract_text(page, "#post-date")

    if link_meta:
        title = title or link_meta.get("title") or ""
        # Prefer ISO timestamp from platform API over DOM-extracted Chinese date
        pub_time = link_meta.get("publish_time") or pub_time or ""

    # ------ Extract content HTML ------
    content_el = await page.query_selector("#js_content")
    if content_el:
        content_html = await content_el.inner_html()
    else:
        content_el = await page.query_selector(".rich_media_content")
        content_html = await content_el.inner_html() if content_el else ""

    if not content_html.strip():
        logger.warning("Empty content for %s", url)

    # Full page HTML for archival
    full_html = await page.content()

    # ------ Images ------
    assets_dir = out_dir / "assets" / aid
    assets_dir.mkdir(parents=True, exist_ok=True)

    image_urls = assets.extract_image_urls(content_html)

    saved_mapping: dict[str, str] = {}
    for img_url in image_urls:
        normalized = img_url.replace("&amp;", "&")
        matched_body = None
        matched_ct = None
        for intercepted_url, (body, ct) in intercepted_images.items():
            if _urls_match(normalized, intercepted_url):
                matched_body = body
                matched_ct = ct
                break
        if matched_body:
            fname = assets.save_intercepted_image(matched_body, normalized, matched_ct, assets_dir)
            saved_mapping[img_url] = fname

    saved_mapping = assets.download_missing_images(image_urls, saved_mapping, assets_dir)

    # ------ Build directory & save ------
    articles_base = out_dir / "articles"
    articles_base.mkdir(parents=True, exist_ok=True)

    dirname = storage.make_article_dirname(pub_time, title, aid)
    existing_dir = storage.find_article_dir(out_dir, aid)

    if existing_dir:
        if existing_dir.name != dirname:
            target = articles_base / dirname
            if not target.exists():
                existing_dir.rename(target)
                logger.info("Renamed %s → %s", existing_dir.name, dirname)
                article_dir = target
            else:
                import shutil
                shutil.rmtree(existing_dir)
                article_dir = target
        else:
            article_dir = existing_dir
    else:
        article_dir = storage.ensure_unique_dir(articles_base, dirname)

    article_dir.mkdir(parents=True, exist_ok=True)

    rel_prefix = f"../../assets/{aid}"

    local_html = assets.rewrite_html(content_html, saved_mapping, rel_prefix)
    local_md = md(local_html, heading_style="ATX", strip=["script", "style"])
    local_md = assets.rewrite_markdown(local_md, saved_mapping, rel_prefix)

    full_html_local = assets.rewrite_html(full_html, saved_mapping, rel_prefix)

    (article_dir / "article.html").write_text(full_html_local, encoding="utf-8")
    (article_dir / "article.md").write_text(
        _build_md_frontmatter(title, author, account_name, pub_time, url) + local_md,
        encoding="utf-8",
    )

    meta = {
        "article_id": aid,
        "url": url,
        "title": title or "",
        "author": author or "",
        "account_name": account_name or "",
        "publish_time": pub_time or "",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "completed": True,
        "image_count": len(saved_mapping),
        "dir": article_dir.name,
    }
    storage.save_meta(article_dir, meta)
    return meta


# ---------------------------------------------------------------------------
# Batch fetch
# ---------------------------------------------------------------------------

async def fetch_all(
    links: list[dict],
    out_dir: Path,
    min_delay: float = 30,
    max_delay: float = 120,
    headless: bool = True,
    force: bool = False,
    max_retries: int = 3,
    cookies: list[dict] | None = None,
) -> None:
    """Fetch all articles from a list of link records."""
    out_dir = Path(out_dir)
    completed_ids = storage.get_completed_ids(out_dir)
    failed_path = out_dir / "failed.jsonl"

    # Oldest first — sort by publish_time ascending
    links = sorted(links, key=lambda x: x.get("publish_time") or "")
    total = len(links)
    skipped = 0
    succeeded = 0
    failed = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx = await browser.new_context(
            user_agent=_WECHAT_UA,
            viewport={"width": 375, "height": 812},
        )
        if cookies:
            await ctx.add_cookies(cookies)
            logger.info("Injected %d cookies for article fetching", len(cookies))

        for idx, link in enumerate(links, 1):
            url = link["url"]
            aid = storage.article_id(url)

            if aid in completed_ids and not force:
                logger.info("[%d/%d] SKIP (already done): %s", idx, total, link.get("title", url)[:60])
                skipped += 1
                continue

            logger.info("[%d/%d] Fetching: %s", idx, total, link.get("title", url)[:60])

            last_exc: Exception | None = None
            for attempt in range(1, max_retries + 1):
                page = await ctx.new_page()
                try:
                    meta = await fetch_article(page, url, out_dir, aid, link_meta=link)
                    completed_ids.add(aid)
                    succeeded += 1
                    logger.info("[%d/%d] OK: %s (%d images)", idx, total, meta.get("title", "")[:40], meta.get("image_count", 0))
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    logger.warning("[%d/%d] Attempt %d failed: %s", idx, total, attempt, exc)
                    backoff = min(2 ** attempt, 30)
                    await asyncio.sleep(backoff)
                finally:
                    await page.close()

            if last_exc is not None:
                failed += 1
                storage.append_jsonl(
                    failed_path,
                    {
                        "url": url,
                        "article_id": aid,
                        "title": link.get("title", ""),
                        "error": str(last_exc),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                )

            delay = random.uniform(min_delay, max_delay)
            await asyncio.sleep(delay)

        await browser.close()

    logger.info(
        "Done. total=%d succeeded=%d skipped=%d failed=%d",
        total, succeeded, skipped, failed,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _extract_text(page: Page, selector: str) -> str:
    el = await page.query_selector(selector)
    if not el:
        return ""
    try:
        return (await el.inner_text()).strip()
    except Exception:
        return ""


async def _check_article_blocked(page: Page) -> bool:
    for selector in (
        "text=请在微信客户端打开",
        "text=环境异常",
        "text=操作频繁",
        "text=访问过于频繁",
    ):
        el = await page.query_selector(selector)
        if el:
            try:
                if await el.is_visible():
                    return True
            except Exception:
                pass
    return False


def _urls_match(a: str, b: str) -> bool:
    """Fuzzy match two image URLs (ignore protocol & amp encoding)."""
    a = a.replace("&amp;", "&").replace("http://", "https://")
    b = b.replace("&amp;", "&").replace("http://", "https://")
    if a == b:
        return True
    a_base = a.split("?")[0]
    b_base = b.split("?")[0]
    return a_base == b_base and a_base != ""


def _build_md_frontmatter(
    title: str | None,
    author: str | None,
    account: str | None,
    pub_time: str | None,
    url: str,
) -> str:
    lines = ["---"]
    lines.append(f"title: \"{(title or '').replace(chr(34), '')}\"")
    if author:
        lines.append(f"author: \"{author}\"")
    if account:
        lines.append(f"account: \"{account}\"")
    if pub_time:
        lines.append(f"date: \"{pub_time}\"")
    lines.append(f"source: \"{url}\"")
    lines.append("---\n")
    return "\n".join(lines)
