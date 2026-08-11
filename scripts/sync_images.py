"""
Bulk-downloads every image referenced by the CDragon metadata JSON and stores
the raw bytes in the `image_cache` Postgres table, so the app can serve
images from the database instead of hotlinking raw.communitydragon.org.

Run manually:
    python sync_images.py

Or on a schedule (e.g. daily cron, or right after each TFT patch) to pick up
new/changed champions, items, traits, and augments:
    0 6 * * * cd /path/to/app && python sync_images.py >> sync_images.log 2>&1

Safe to re-run: existing URLs are skipped unless --force is passed, and
INSERTs use ON CONFLICT DO UPDATE so re-downloading never duplicates rows.
"""

import argparse
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from db import (
    get_cached_cdragon,
    save_cdragon,
    save_image,
    get_image_cache_stats,
    get_image_cache_urls,
)

CD_BASE = "https://raw.communitydragon.org/latest/"
JSON_URL = f"{CD_BASE}cdragon/tft/en_us.json"
PLUGIN_ROOT = f"{CD_BASE}plugins/rcp-be-lol-game-data/global/default/"

REQUEST_TIMEOUT = 15
MAX_WORKERS = 8
RETRIES = 3
REQUEST_DELAY = 0.3  # seconds, spacing between requests to avoid tripping burst/connection-rate throttling


def clean_path(raw_path):
    """Mirrors cdragon.py's clean_path so URLs match exactly."""
    if not raw_path:
        return None
    path = raw_path.lower().strip()
    prefix = "/lol-game-data/assets/"
    if prefix in path:
        path = path.split(prefix, 1)[1]
    else:
        path = path.lstrip("/")
    if path.endswith(".tex"):
        path = path[:-4] + ".png"
    return PLUGIN_ROOT + path


def current_set_number(data: dict) -> int:
    """setData entries include every set CDragon has record of. We only want
    the live one, both to cut failed downloads and to avoid caching artwork
    nobody will see. Picks the highest numeric set found."""
    numbers = []
    for set_data in data.get("setData", []):
        n = set_data.get("number")
        if isinstance(n, int):
            numbers.append(n)
    return max(numbers) if numbers else None


def collect_all_image_urls(data: dict, current_only: bool = True) -> set:
    """Walk the metadata JSON the same way cdragon.py does and gather every icon URL."""
    urls = set()
    target_set = current_set_number(data) if current_only else None

    for set_data in data.get("setData", []):
        if target_set is not None and set_data.get("number") != target_set:
            continue

        for champ in set_data.get("champions", []):
            icon_path = champ.get("tileIcon") or champ.get("squareIcon")
            url = clean_path(icon_path)
            if url:
                urls.add(url)

        for trait in set_data.get("traits", []):
            url = clean_path(trait.get("icon"))
            if url:
                urls.add(url)

    for item in data.get("items", []):
        url = clean_path(item.get("icon"))
        if url:
            urls.add(url)

    return urls


TIER_SUFFIX_RE = re.compile(r"([_-])(i{1,3})(\.png)$")


def _strip_tier_suffix(url: str):
    """Some augment icons are referenced per-tier (_i/_ii/_iii or -i/-ii/-iii)
    but only exist on disk as a single shared icon. Returns the de-suffixed
    URL, or None if the URL has no tier suffix to strip."""
    m = TIER_SUFFIX_RE.search(url)
    if not m:
        return None
    return url[: m.start()] + ".png"


PLUGIN_TREE_PREFIX = "plugins/rcp-be-lol-game-data/global/default/assets/"
GAME_TREE_PREFIX = "game/assets/"


def _game_tree_variant(url: str):
    """Augment icons and 'particle' item overlays (e.g. .../maps/particles/tft/...,
    .../maps/tft/icons/augments/...) commonly live under the raw game-client
    tree (game/assets/...) rather than the LCU plugin tree
    (plugins/rcp-be-lol-game-data/global/default/assets/...) that regular
    item/champion icons use. Returns the game/ tree equivalent, or None if
    this URL isn't under the plugin tree to begin with."""
    if PLUGIN_TREE_PREFIX not in url:
        return None
    base, _, rest = url.partition(PLUGIN_TREE_PREFIX)
    return base + GAME_TREE_PREFIX + rest


def _get(url: str, timeout: int = REQUEST_TIMEOUT):
    time.sleep(REQUEST_DELAY)
    res = requests.get(url, timeout=timeout)
    res.raise_for_status()
    content_type = res.headers.get("Content-Type", "image/png")
    return content_type, res.content


def _candidate_urls(url: str):
    """Build the ordered list of URLs to try for a single logical image:
    original -> game/ tree variant -> tier-suffix stripped -> both combined."""
    candidates = [url]

    game_variant = _game_tree_variant(url)
    if game_variant:
        candidates.append(game_variant)

    stripped = _strip_tier_suffix(url)
    if stripped:
        candidates.append(stripped)
        if game_variant:
            stripped_game_variant = _game_tree_variant(stripped)
            if stripped_game_variant:
                candidates.append(stripped_game_variant)

    return candidates


def fetch_one(url: str):
    """Download a single image. Returns (url, content_type, data, error).

    Tries each candidate path in order (see _candidate_urls) since CDragon
    splits TFT art across two separate asset trees (plugin tree for regular
    item/champion icons, game/ tree for augments and particle-based item
    overlays) and some augment icons reference a per-tier filename that only
    exists as a shared, non-suffixed file. A 404 moves on to the next
    candidate immediately; other errors (timeouts, 5xx, connection resets)
    get retried with backoff before moving on.
    """
    last_err = None
    for candidate in _candidate_urls(url):
        for attempt in range(RETRIES + 1):
            try:
                content_type, content = _get(candidate)
                return url, content_type, content, None
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                last_err = e
                if status == 404:
                    break  # try next candidate, no point retrying a 404
                time.sleep(1.5 * (attempt + 1))  # 429/5xx: back off and retry
            except Exception as e:
                last_err = e
                time.sleep(0.5 * (attempt + 1))
    return url, None, None, last_err


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true",
        help="Re-download every image even if already cached in the DB."
    )
    args = parser.parse_args()

    print("Fetching CDragon metadata JSON...")
    data = get_cached_cdragon()
    if not data:
        res = requests.get(JSON_URL, timeout=REQUEST_TIMEOUT)
        res.raise_for_status()
        data = res.json()
        save_cdragon(data)
        print("  (fetched fresh, since local cache was empty/stale)")
    else:
        print("  (used cached metadata JSON; run with a fresh cdragon cache if you patched recently)")

    set_num = current_set_number(data)
    if set_num is not None:
        print(f"Restricting to current set (TFT set {set_num}) -- older sets' assets "
              f"are usually no longer served under /latest/ and would just 404.")
    else:
        print("Could not determine current set number -- pulling images for all sets found.")

    all_urls = collect_all_image_urls(data, current_only=(set_num is not None))
    print(f"Found {len(all_urls)} unique image URLs referenced in metadata.")

    if args.force:
        to_fetch = list(all_urls)
    else:
        print("Checking which images are already cached (single batch query)...")
        already_cached = get_image_cache_urls()
        to_fetch = [u for u in all_urls if u not in already_cached]

    print(f"Downloading {len(to_fetch)} images ({len(all_urls) - len(to_fetch)} already cached)...")

    ok, failed = 0, []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_one, url): url for url in to_fetch}
        for i, future in enumerate(as_completed(futures), 1):
            url, content_type, content, err = future.result()
            if err:
                failed.append((url, str(err)))
                print(f"  [{i}/{len(to_fetch)}] FAIL  {type(err).__name__}: {err}\n         {url}")
            else:
                save_image(url, content_type, content)
                ok += 1
                if i % 25 == 0 or i == len(to_fetch):
                    print(f"  [{i}/{len(to_fetch)}] ok so far: {ok}, failed so far: {len(failed)}")

    count, total_bytes = get_image_cache_stats()
    print(f"\nDone. {ok} images downloaded/updated, {len(failed)} failed.")
    print(f"image_cache now holds {count} images, {total_bytes / 1024 / 1024:.1f} MB total.")

    if failed:
        print("\nFailed URLs:")
        for url, err in failed[:20]:
            print(f"  {url}\n    {type(err).__name__}: {err}")
        if len(failed) > 20:
            print(f"  ...and {len(failed) - 20} more")
        sys.exit(1)


if __name__ == "__main__":
    main()