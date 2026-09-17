#!/usr/bin/env python3
"""
Scrape FUTBIN player-listing pages (e.g. https://www.futbin.com/26/players)
and collect the detail-page link for every player found
(e.g. https://www.futbin.com/26/player/25561/rodrigo-hernandez-cascante).

Designed to walk all paginated listing pages (``?page=N``), and to be safe
to interrupt and resume: progress is checkpointed after every page, and the
output CSV is only ever appended to once a page has been fully parsed.

Usage:
    python -m futbin_scraper.scraper --start-page 1 --end-page 952

Run ``python -m futbin_scraper.scraper --help`` for all options.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from threading import Lock
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger("futbin_scraper")

DEFAULT_BASE_URL = "https://www.futbin.com/26/players"
DEFAULT_OUTPUT = "output/players.csv"
DEFAULT_PROGRESS_FILE = "output/progress.json"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Matches links like /26/player/25561/rodrigo-hernandez-cascante
PLAYER_LINK_RE = re.compile(r"/(\d+)/player/(\d+)/([a-z0-9\-]+)", re.IGNORECASE)
# Matches ?page=N or &page=N anywhere in the HTML (used to auto-detect the
# last page number from the listing page's own pagination controls).
PAGE_PARAM_RE = re.compile(r"[?&]page=(\d+)")


@dataclass
class PlayerLink:
    player_id: str
    name: str
    url: str
    source_page: int


def build_session(user_agent: str, max_retries: int) -> requests.Session:
    """A requests session with retries for transient network/server errors.

    403/429 (bot-detection / rate-limiting) are handled separately in
    ``fetch_page`` with a longer, logged backoff rather than silently via
    urllib3, since those responses usually mean "slow down", not "retry
    immediately".
    """
    session = requests.Session()
    retry = Retry(
        total=max_retries,
        connect=max_retries,
        read=max_retries,
        backoff_factor=1.5,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return session


def fetch_page(
    session: requests.Session,
    url: str,
    timeout: float,
    max_retries: int,
) -> str | None:
    """GET a URL, retrying with backoff on connection errors, 403 and 429.

    Returns the response text, or None if it could not be fetched after
    ``max_retries`` attempts (the caller is expected to log and skip it).
    """
    attempt = 0
    while attempt <= max_retries:
        attempt += 1
        try:
            resp = session.get(url, timeout=timeout)
        except requests.RequestException as exc:
            LOG.warning("Request error on %s (attempt %d/%d): %s", url, attempt, max_retries, exc)
            time.sleep(min(60, 2 ** attempt))
            continue

        if resp.status_code == 200:
            return resp.text

        if resp.status_code in (403, 429):
            wait = min(120, 5 * (2 ** attempt))
            LOG.warning(
                "Got HTTP %d for %s (attempt %d/%d), backing off %ds",
                resp.status_code, url, attempt, max_retries, wait,
            )
            time.sleep(wait)
            continue

        LOG.warning("Unexpected HTTP %d for %s (attempt %d/%d)", resp.status_code, url, attempt, max_retries)
        time.sleep(min(30, 2 ** attempt))

    LOG.error("Giving up on %s after %d attempts", url, max_retries)
    return None


def extract_player_links(html: str, base_url: str, page_num: int) -> list[PlayerLink]:
    """Pull every unique player detail-page link out of a listing page.

    Matching is done on the href pattern itself (``/<year>/player/<id>/<slug>``)
    rather than on any particular table/CSS structure, so it keeps working
    even if FUTBIN's markup around the table changes. A player can appear
    twice in a row (e.g. once on the thumbnail, once on the name) -- the
    first non-empty text found is kept as the name.
    """
    soup = BeautifulSoup(html, "lxml")
    seen: dict[str, PlayerLink] = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        match = PLAYER_LINK_RE.search(href)
        if not match:
            continue
        _year, player_id, slug = match.groups()
        path = f"/{_year}/player/{player_id}/{slug}"
        full_url = urljoin(base_url, path)
        name = a.get_text(strip=True)

        existing = seen.get(player_id)
        if existing is None:
            seen[player_id] = PlayerLink(player_id, name, full_url, page_num)
        elif not existing.name and name:
            existing.name = name

    return list(seen.values())


def detect_last_page(html: str) -> int | None:
    """Best-effort detection of the final page number from pagination links."""
    numbers = [int(n) for n in PAGE_PARAM_RE.findall(html)]
    return max(numbers) if numbers else None


def page_url(base_url: str, page_num: int) -> str:
    if page_num <= 1:
        return base_url
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}page={page_num}"


class ProgressState:
    """Tracks which pages have been fully scraped, so a run can be resumed."""

    def __init__(self, path: str):
        self.path = path
        self.completed_pages: set[int] = set()
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.completed_pages = set(data.get("completed_pages", []))

    def mark_done(self, page_num: int) -> None:
        self.completed_pages.add(page_num)
        self._save()

    def _save(self) -> None:
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"completed_pages": sorted(self.completed_pages)}, f)
        os.replace(tmp_path, self.path)

    def is_done(self, page_num: int) -> bool:
        return page_num in self.completed_pages


class ResultWriter:
    """Thread-safe, append-only CSV writer with cross-run de-duplication."""

    def __init__(self, path: str):
        self.path = path
        self._lock = Lock()
        self._seen_ids: set[str] = set()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._load_existing()
        self._file = open(path, "a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        if self._file.tell() == 0:
            self._writer.writerow(["player_id", "name", "url", "source_page"])
            self._file.flush()

    def _load_existing(self) -> None:
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)
            for row in reader:
                if row:
                    self._seen_ids.add(row[0])

    def write(self, links: Iterable[PlayerLink]) -> int:
        written = 0
        with self._lock:
            for link in links:
                if link.player_id in self._seen_ids:
                    continue
                self._seen_ids.add(link.player_id)
                self._writer.writerow([link.player_id, link.name, link.url, link.source_page])
                written += 1
            if written:
                self._file.flush()
        return written

    def close(self) -> None:
        self._file.close()


def scrape_page(
    session: requests.Session,
    base_url: str,
    page_num: int,
    timeout: float,
    max_retries: int,
) -> tuple[int, list[PlayerLink] | None]:
    url = page_url(base_url, page_num)
    html = fetch_page(session, url, timeout, max_retries)
    if html is None:
        return page_num, None
    return page_num, extract_player_links(html, base_url, page_num)


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    session = build_session(args.user_agent, args.max_retries)

    start_page = args.start_page
    end_page = args.end_page

    if end_page is None:
        LOG.info("No --end-page given, fetching page 1 to auto-detect the last page number")
        first_html = fetch_page(session, page_url(args.base_url, 1), args.timeout, args.max_retries)
        detected = detect_last_page(first_html) if first_html else None
        end_page = detected or 952
        LOG.info("Using end page %d%s", end_page, " (auto-detected)" if detected else " (fallback default)")

    progress = ProgressState(args.progress_file)
    writer = ResultWriter(args.output)

    pages = [p for p in range(start_page, end_page + 1) if not progress.is_done(p)]
    total = end_page - start_page + 1
    skipped = total - len(pages)
    if skipped:
        LOG.info("Resuming: skipping %d already-completed page(s)", skipped)
    LOG.info("Scraping %d page(s) from %s (pages %d-%d)", len(pages), args.base_url, start_page, end_page)

    failed_pages: list[int] = []
    total_written = 0
    done_count = 0

    def handle_result(page_num: int, links: list[PlayerLink] | None) -> None:
        nonlocal total_written, done_count
        if links is None:
            failed_pages.append(page_num)
            return
        total_written += writer.write(links)
        progress.mark_done(page_num)
        done_count += 1
        if done_count % 25 == 0 or done_count == len(pages):
            LOG.info(
                "Progress: %d/%d pages done, %d player links written so far",
                done_count, len(pages), total_written,
            )

    try:
        if args.workers <= 1:
            for page_num in pages:
                _, links = scrape_page(session, args.base_url, page_num, args.timeout, args.max_retries)
                handle_result(page_num, links)
                time.sleep(args.delay + random.uniform(0, args.jitter))
        else:
            def worker(page_num: int) -> tuple[int, list[PlayerLink] | None]:
                result = scrape_page(session, args.base_url, page_num, args.timeout, args.max_retries)
                time.sleep(args.delay + random.uniform(0, args.jitter))
                return result

            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(worker, p): p for p in pages}
                for future in as_completed(futures):
                    page_num, links = future.result()
                    handle_result(page_num, links)
    finally:
        writer.close()

    if failed_pages:
        failed_path = f"{args.output}.failed_pages.txt"
        with open(failed_path, "w", encoding="utf-8") as f:
            f.write("\n".join(str(p) for p in sorted(failed_pages)))
        LOG.warning(
            "%d page(s) failed after retries; page numbers written to %s. "
            "Re-run with --start-page/--end-page covering those pages to retry them "
            "(already-completed pages are skipped automatically).",
            len(failed_pages), failed_path,
        )

    LOG.info("Done. %d total unique player links in %s", total_written, args.output)
    return 1 if failed_pages else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Listing page base URL (default: %(default)s)")
    parser.add_argument("--start-page", type=int, default=1, help="First page to scrape (default: %(default)s)")
    parser.add_argument(
        "--end-page", type=int, default=None,
        help="Last page to scrape. If omitted, it is auto-detected from the listing page's own "
             "pagination controls, falling back to 952 if that fails.",
    )
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output CSV path (default: %(default)s)")
    parser.add_argument("--progress-file", default=DEFAULT_PROGRESS_FILE, help="Resume checkpoint file (default: %(default)s)")
    parser.add_argument("--delay", type=float, default=1.5, help="Base delay in seconds between requests per worker (default: %(default)s)")
    parser.add_argument("--jitter", type=float, default=1.0, help="Extra random delay (0-jitter seconds) added to --delay (default: %(default)s)")
    parser.add_argument("--timeout", type=float, default=20.0, help="Per-request timeout in seconds (default: %(default)s)")
    parser.add_argument("--max-retries", type=int, default=5, help="Max retries per page before giving up on it (default: %(default)s)")
    parser.add_argument("--workers", type=int, default=1, help="Number of concurrent workers (default: %(default)s, i.e. sequential)")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="User-Agent header to send")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose (debug) logging")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
