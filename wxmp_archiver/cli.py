"""CLI entry-point for wxmp_archiver (Typer)."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import typer
from rich.logging import RichHandler

from . import storage

app = typer.Typer(
    name="wxmp_archiver",
    help="Archive WeChat Official Account articles offline (Markdown/HTML + images).",
    add_completion=False,
)


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
    )


# ---------------------------------------------------------------------------
# crawl-links
# ---------------------------------------------------------------------------

@app.command("crawl-links")
def crawl_links(
    history_url: str = typer.Option(..., "--history-url", help="WeChat profile_ext history URL"),
    out: str = typer.Option("./out", "--out", help="Output directory"),
    max_pages: int = typer.Option(999, "--max-pages", help="Max pagination rounds"),
    headless: bool = typer.Option(False, "--headless", help="Run browser in headless mode"),
    cookie: str = typer.Option("", "--cookie", help="(Advanced) Cookie string/file — skip QR login, use profile_ext directly"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Collect all article links from a WeChat Official Account.

    Default: opens mp.weixin.qq.com for QR code login, then enumerates via API.
    With --cookie: opens profile_ext page directly using injected cookies.
    """
    _setup_logging(verbose)
    from .history import crawl_links as _crawl

    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cookies = storage.load_cookies(cookie)

    if not cookies:
        typer.echo(
            "No --cookie provided → will use QR code login.\n"
            "A browser window will open — please scan the QR code with WeChat.\n"
            "(Requires a WeChat Official Account, personal subscription accounts work)\n"
        )

    links = asyncio.run(
        _crawl(history_url, out_dir, max_pages=max_pages, headless=headless, cookies=cookies)
    )
    typer.echo(f"\n✓ Collected {len(links)} links → {out_dir / 'links.jsonl'}")


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------

@app.command("fetch")
def fetch(
    links: str = typer.Option("./out/links.jsonl", "--links", help="Path to links.jsonl"),
    out: str = typer.Option("./out", "--out", help="Output directory"),
    min_delay: float = typer.Option(30.0, "--min-delay", help="Min seconds between requests"),
    max_delay: float = typer.Option(120.0, "--max-delay", help="Max seconds between requests"),
    headless: bool = typer.Option(True, "--headless", help="Run browser in headless mode"),
    resume: bool = typer.Option(True, "--resume", help="Skip already-completed articles"),
    force: bool = typer.Option(False, "--force", help="Re-fetch even if already completed"),
    cookie: str = typer.Option("", "--cookie", help="Cookie string or path to cookie file"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Fetch articles from links.jsonl, save HTML/Markdown/images locally."""
    _setup_logging(verbose)
    from .article import fetch_all

    links_path = Path(links)
    if not links_path.exists():
        typer.echo(f"Error: {links_path} not found. Run crawl-links first.", err=True)
        raise typer.Exit(1)

    records = storage.read_jsonl(links_path)
    if not records:
        typer.echo("No links found in links.jsonl.")
        raise typer.Exit(0)

    out_dir = Path(out)
    cookies = storage.load_cookies(cookie)
    typer.echo(f"Fetching {len(records)} articles → {out_dir}")

    asyncio.run(
        fetch_all(
            records,
            out_dir,
            min_delay=min_delay,
            max_delay=max_delay,
            headless=headless,
            force=force,
            cookies=cookies,
        )
    )
    typer.echo("\n✓ Fetch complete.")


# ---------------------------------------------------------------------------
# run  (crawl-links + fetch in one go)
# ---------------------------------------------------------------------------

@app.command("run")
def run(
    history_url: str = typer.Option(..., "--history-url", help="WeChat profile_ext history URL"),
    out: str = typer.Option("./out", "--out", help="Output directory"),
    max_pages: int = typer.Option(999, "--max-pages", help="Max pagination rounds for link crawling"),
    headless: bool = typer.Option(False, "--headless", help="Run browser in headless mode"),
    min_delay: float = typer.Option(30.0, "--min-delay", help="Min seconds between requests"),
    max_delay: float = typer.Option(120.0, "--max-delay", help="Max seconds between requests"),
    force: bool = typer.Option(False, "--force", help="Re-fetch even if already completed"),
    cookie: str = typer.Option("", "--cookie", help="(Advanced) Cookie string/file — skip QR login"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """One-shot: crawl links then fetch all articles."""
    _setup_logging(verbose)
    from .article import fetch_all
    from .history import crawl_links as _crawl

    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cookies = storage.load_cookies(cookie)

    typer.echo("=== Phase 1: Crawling article links ===")
    if not cookies:
        typer.echo(
            "A browser will open for QR code login — scan with WeChat to continue.\n"
        )
    links = asyncio.run(
        _crawl(history_url, out_dir, max_pages=max_pages, headless=headless, cookies=cookies)
    )
    typer.echo(f"✓ {len(links)} links collected\n")

    if not links:
        typer.echo("No links found — nothing to fetch.")
        raise typer.Exit(0)

    typer.echo("=== Phase 2: Fetching articles ===")
    asyncio.run(
        fetch_all(
            links,
            out_dir,
            min_delay=min_delay,
            max_delay=max_delay,
            headless=headless,
            force=force,
            cookies=cookies,
        )
    )
    typer.echo("\n✓ All done.")


# ---------------------------------------------------------------------------
# retry-failed
# ---------------------------------------------------------------------------

@app.command("retry-failed")
def retry_failed(
    out: str = typer.Option("./out", "--out", help="Output directory"),
    min_delay: float = typer.Option(30.0, "--min-delay"),
    max_delay: float = typer.Option(120.0, "--max-delay"),
    headless: bool = typer.Option(True, "--headless"),
    cookie: str = typer.Option("", "--cookie", help="Cookie string or path to cookie file"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Re-fetch articles that previously failed (from failed.jsonl)."""
    _setup_logging(verbose)
    from .article import fetch_all

    out_dir = Path(out)
    failed_path = out_dir / "failed.jsonl"
    if not failed_path.exists():
        typer.echo("No failed.jsonl found — nothing to retry.")
        raise typer.Exit(0)

    records = storage.read_jsonl(failed_path)
    if not records:
        typer.echo("failed.jsonl is empty.")
        raise typer.Exit(0)

    seen: set[str] = set()
    unique: list[dict] = []
    for r in records:
        if r["url"] not in seen:
            seen.add(r["url"])
            unique.append(r)

    cookies = storage.load_cookies(cookie)
    typer.echo(f"Retrying {len(unique)} failed articles…")

    failed_path.rename(failed_path.with_suffix(".jsonl.bak"))

    asyncio.run(
        fetch_all(
            unique,
            out_dir,
            min_delay=min_delay,
            max_delay=max_delay,
            headless=headless,
            force=True,
            cookies=cookies,
        )
    )
    typer.echo("\n✓ Retry complete.")


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main() -> None:
    app()


if __name__ == "__main__":
    main()
