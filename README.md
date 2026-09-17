# FUTBIN Player Link Scraper

Walks the FUTBIN player-listing pages (e.g. `https://www.futbin.com/26/players`,
`https://www.futbin.com/26/players?page=2`, ...) and collects the detail-page
URL for every player listed (e.g.
`https://www.futbin.com/26/player/25561/rodrigo-hernandez-cascante`), writing
them to a CSV file.

## Install

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Scrape all pages (auto-detects the last page number from the site's own
# pagination controls; falls back to 952 if that fails)
python -m futbin_scraper.scraper

# Or specify an explicit page range
python -m futbin_scraper.scraper --start-page 1 --end-page 952

# Custom output location
python -m futbin_scraper.scraper --output output/players.csv
```

Output is a CSV with columns: `player_id, name, url, source_page`.

Run `python -m futbin_scraper.scraper --help` for all options (delay,
timeout, retries, concurrency, user agent, etc).

## Resuming an interrupted run

Progress is checkpointed to `output/progress.json` after every successfully
scraped page, and results are appended to the output CSV as they come in
(a player already present in the CSV is never written twice). If the run
is interrupted (Ctrl-C, network drop, etc.), just re-run the same command:
already-completed pages are skipped automatically.

If some pages fail even after retries, their page numbers are written to
`<output>.failed_pages.txt`. Re-run with `--start-page`/`--end-page`
covering those pages to retry just them.

## Being polite to the site

By default the scraper waits `--delay` (1.5s) plus a random `--jitter`
(0-1s) between requests, and backs off with increasing delays on
HTTP 403/429 responses instead of hammering the server. Before running a
full 952-page scrape, check FUTBIN's `robots.txt` and terms of service
yourself and adjust `--delay`/`--workers` to stay within whatever they
allow.

Optional `--workers N` runs N requests concurrently (each still respecting
`--delay`/`--jitter` individually) if you want to speed things up and have
confirmed that's acceptable.

## Testing

The HTML parsing logic (link extraction, pagination detection, URL
building) has unit tests that run against a local fixture, with no network
access required:

```bash
python -m unittest discover -s tests -v
```

**Note:** this scraper was developed in a sandboxed environment with no
outbound network access to futbin.com, so the parsing logic could only be
validated against a hand-built fixture page and a local test HTTP server,
not against the live site's actual markup. The link-matching regex
(`/<year>/player/<id>/<slug>`) only depends on the `href` values themselves,
not on any particular table/CSS structure, which should make it resilient
to markup differences, but please do a small test run first
(e.g. `--start-page 1 --end-page 2`) and check `output/players.csv` looks
right before kicking off the full 952-page scrape.
