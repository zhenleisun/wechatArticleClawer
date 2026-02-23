"""Storage utilities: article IDs, directory naming, JSONL I/O, resume logic, cookie parsing."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


def article_id(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()


def slugify(text: str, max_len: int = 50) -> str:
    """Filesystem-safe slug that preserves CJK characters."""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text)
    text = text.strip().replace(" ", "_")
    if len(text) > max_len:
        text = text[:max_len].rstrip("_")
    return text or "untitled"


def make_article_dirname(
    publish_time: str | None, title: str | None, aid: str
) -> str:
    slug = slugify(title) if title else aid[:12]
    if publish_time:
        # ISO format: "2024-05-31T21:29:15"
        try:
            dt = datetime.fromisoformat(publish_time)
            return f"{dt.strftime('%Y%m%d')}_{slug}"
        except (ValueError, TypeError):
            pass
        # Chinese format: "2024年6月14日 16:35"
        m = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", publish_time)
        if m:
            prefix = f"{m.group(1)}{m.group(2).zfill(2)}{m.group(3).zfill(2)}"
            return f"{prefix}_{slug}"
    return slug


def get_completed_ids(out_dir: Path) -> set[str]:
    """Scan all meta.json under articles/ to find completed article IDs."""
    completed: set[str] = set()
    articles_dir = out_dir / "articles"
    if not articles_dir.exists():
        return completed
    for meta_path in articles_dir.glob("*/meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("completed"):
                completed.add(meta["article_id"])
        except (json.JSONDecodeError, KeyError, OSError):
            continue
    return completed


def find_article_dir(out_dir: Path, aid: str) -> Path | None:
    articles_dir = out_dir / "articles"
    if not articles_dir.exists():
        return None
    for meta_path in articles_dir.glob("*/meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("article_id") == aid:
                return meta_path.parent
        except (json.JSONDecodeError, KeyError, OSError):
            continue
    return None


def ensure_unique_dir(base_dir: Path, dirname: str) -> Path:
    target = base_dir / dirname
    if not target.exists():
        return target
    for i in range(2, 1000):
        candidate = base_dir / f"{dirname}_{i}"
        if not candidate.exists():
            return candidate
    return base_dir / f"{dirname}_{hashlib.sha1(dirname.encode()).hexdigest()[:8]}"


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def save_meta(article_dir: Path, meta: dict) -> None:
    article_dir.mkdir(parents=True, exist_ok=True)
    with open(article_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------

def parse_cookie_string(raw: str) -> list[dict]:
    """Parse a raw cookie header string (``name=val; name2=val2``) into
    Playwright-compatible cookie dicts for ``.qq.com`` domain."""
    cookies: list[dict] = []
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        cookies.append(
            {
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".qq.com",
                "path": "/",
            }
        )
    return cookies


def load_cookies(cookie_arg: str | None) -> list[dict]:
    """Load cookies from a file path or raw string.

    - If *cookie_arg* points to an existing file, read it (one cookie-header
      per line, or a single ``Cookie: ...`` header line).
    - Otherwise treat it as a raw cookie string.
    - Returns an empty list if *cookie_arg* is None/empty.
    """
    if not cookie_arg:
        return []

    path = Path(cookie_arg)
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        if text.lower().startswith("cookie:"):
            text = text.split(":", 1)[1].strip()
        return parse_cookie_string(text)

    return parse_cookie_string(cookie_arg)
