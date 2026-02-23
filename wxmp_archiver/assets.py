"""Download images and rewrite links in HTML / Markdown to local relative paths."""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


def _guess_ext(url: str, content_type: str | None = None) -> str:
    if content_type:
        ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if ext:
            return ext
    path = urlparse(url).path
    for candidate in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp"):
        if candidate in path.lower():
            return candidate
    if "wx_fmt=png" in url:
        return ".png"
    if "wx_fmt=gif" in url:
        return ".gif"
    if "wx_fmt=svg" in url:
        return ".svg"
    return ".jpg"


def _image_filename(url: str, content_type: str | None = None) -> str:
    url_hash = hashlib.sha1(url.encode()).hexdigest()[:16]
    ext = _guess_ext(url, content_type)
    return f"{url_hash}{ext}"


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10), reraise=True)
def _download_image(url: str, dest: Path, timeout: float = 30) -> None:
    """Download a single image via httpx with retries."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        dest.write_bytes(resp.content)


def save_intercepted_image(
    body: bytes,
    url: str,
    content_type: str | None,
    assets_dir: Path,
) -> str:
    """Save an image captured from Playwright network interception.
    Returns the local filename."""
    fname = _image_filename(url, content_type)
    dest = assets_dir / fname
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
    return fname


def download_missing_images(
    image_urls: list[str],
    already_saved: dict[str, str],
    assets_dir: Path,
) -> dict[str, str]:
    """Download images not already captured via interception.

    Returns mapping of ``original_url -> local_filename`` (merged with already_saved).
    """
    mapping = dict(already_saved)
    for url in image_urls:
        if url in mapping:
            continue
        fname = _image_filename(url)
        dest = assets_dir / fname
        if dest.exists():
            mapping[url] = fname
            continue
        try:
            _download_image(url, dest)
            mapping[url] = fname
            logger.debug("Downloaded image: %s", url)
        except Exception as exc:
            logger.warning("Failed to download image %s: %s", url, exc)
    return mapping


# ---------------------------------------------------------------------------
# Link rewriting
# ---------------------------------------------------------------------------

def _relative_path(article_dir: Path, assets_dir: Path) -> str:
    """Compute the relative path from article_dir to assets_dir."""
    try:
        return str(assets_dir.relative_to(article_dir))
    except ValueError:
        return str(Path(*(['..'] * len(article_dir.parts))) / assets_dir)


def rewrite_html(
    html: str,
    url_to_local: dict[str, str],
    rel_prefix: str,
) -> str:
    """Replace image src in HTML with local relative paths."""
    for orig_url, local_name in url_to_local.items():
        local_ref = f"{rel_prefix}/{local_name}"
        html = html.replace(orig_url, local_ref)
        escaped = orig_url.replace("&", "&amp;")
        if escaped != orig_url:
            html = html.replace(escaped, local_ref)
    return html


def rewrite_markdown(
    md: str,
    url_to_local: dict[str, str],
    rel_prefix: str,
) -> str:
    """Replace image URLs in Markdown with local relative paths."""
    for orig_url, local_name in url_to_local.items():
        local_ref = f"{rel_prefix}/{local_name}"
        md = md.replace(orig_url, local_ref)
        escaped = orig_url.replace("&", "&amp;")
        if escaped != orig_url:
            md = md.replace(escaped, local_ref)
    return md


def extract_image_urls(html: str) -> list[str]:
    """Extract all image URLs from HTML content."""
    urls: list[str] = []
    seen: set[str] = set()

    for pattern in (
        r'<img[^>]+src=["\']([^"\']+)["\']',
        r'data-src=["\']([^"\']+)["\']',
        r'data-original=["\']([^"\']+)["\']',
    ):
        for match in re.finditer(pattern, html, re.IGNORECASE):
            url = match.group(1).replace("&amp;", "&")
            if url and url not in seen and url.startswith("http"):
                seen.add(url)
                urls.append(url)
    return urls
