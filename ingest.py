#!/usr/bin/env python3
"""
Trove ingest pipeline: Rebrickable + Brickset → catalog.json
Schema: { "version": N, "items": [...] }

Modes:
  --sample   use fixtures/*.json instead of live APIs (no keys needed)
  (default)  live mode; requires REBRICKABLE_API_KEY + BRICKSET_API_KEY env vars

Attribution: data from Rebrickable (rebrickable.com) and Brickset (brickset.com).
"""
# ponytail: stdlib HTTP only — no requests, no pandas, no third-party deps

import datetime as _dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _urlopen_json(req, timeout: int = 30, attempts: int = 4) -> dict:
    """urlopen + json with retry/backoff. ponytail: covers transient TLS/network
    blips (e.g. SSL UNEXPECTED_EOF) that otherwise abort a whole theme/page."""
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            # 429 = rate limited: honour Retry-After (or back off) and retry.
            # ponytail: Rebrickable's free tier throttles bursts; without this the
            # whole theme's pagination aborts and its sets vanish from the catalog.
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", 0) or 0) or (2 * (i + 1))
                print(f"  429 rate-limited — sleeping {wait}s (attempt {i})", file=sys.stderr)
                time.sleep(wait)
                last_err = e
                continue
            raise  # other 4xx/5xx are real — don't mask them
        except Exception as e:  # noqa: BLE001 - transient transport errors only
            last_err = e
            time.sleep(1.5 * (i + 1))
    raise last_err  # type: ignore[misc]

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
OUT = HERE / "catalog.json"
OVERRIDES_PATH = HERE / "overrides" / "retiring.json"
CACHE_PATH = HERE / "cache" / "retiring_last_good.json"
STATUS_PATH = HERE / "status.json"
CACHE_MAX_AGE_DAYS = 21  # older than this, the last-known-good cache is not trusted
VERSION = 2  # bump when schema changes

# Refuse to overwrite the published catalog with a near-empty result (a bad run
# must never wipe good data). ponytail: simple floor, not a diff-ratio check.
MIN_ITEMS = 5

# ---------------------------------------------------------------------------
# Rebrickable helpers
# ---------------------------------------------------------------------------

RB_BASE = "https://rebrickable.com/api/v3/lego"


def rb_get(path: str, key: str, params: dict | None = None) -> dict:
    p = dict(params or {})
    p["key"] = key
    url = f"{RB_BASE}{path}?{urllib.parse.urlencode(p)}"
    # ponytail: crude global throttle — Rebrickable's free tier rejects bursts with
    # 429. 1 req/s keeps us under the limit; the all-years pull is ~150 calls (~3 min).
    time.sleep(1.0)
    return _urlopen_json(urllib.request.Request(url))


def _load_themes_raw(key: str) -> list[dict]:
    """Return full Rebrickable theme list [{id, name, parent_id}, ...] via paginated pull.

    ponytail: one paginated pull (page_size 1000 fits all ~500 themes in one page).
    Per-page error handling so a bad page never kills the run.
    """
    # ponytail: page_size 1000 returns all ~494 themes in one page. The theme list
    # is load-bearing — a partial pull silently drops subthemes (e.g. Star Wars UCS),
    # collapsing whole-theme coverage. So verify against the API's `count` and retry;
    # fail loudly rather than emit a truncated list that quietly wrecks the catalog.
    for attempt in range(4):
        themes: list[dict] = []
        expected: int | None = None
        page = 1
        ok = True
        while page <= 10:
            try:
                data = rb_get("/themes/", key, {"page": page, "page_size": 1000})
            except Exception as e:  # noqa: BLE001
                print(f"  WARN: theme list page {page} attempt {attempt} failed: {e}",
                      file=sys.stderr)
                ok = False
                break
            expected = data.get("count", expected)
            themes.extend(data.get("results", []))
            if not data.get("next"):
                break
            page += 1
        if ok and expected is not None and len(themes) >= expected:
            return themes
        print(f"  WARN: theme list incomplete ({len(themes)}/{expected}) — retrying",
              file=sys.stderr)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"theme list incomplete after retries: {len(themes)}/{expected}")


def build_id_to_root_name(themes: list[dict]) -> dict[int, str]:
    """Return {theme_id -> root theme name} for every theme in the list.

    Root = the ancestor whose parent_id is None. Each theme id maps to its root's name.
    Cycle-guarded via a per-walk visited set; unresolvable themes (dangling parent or
    cycle with no root) are omitted from the result → callers default to "".

    ponytail: same lineage-walk pattern as the old build_id_to_top_name, but always
    walks to the true root instead of stopping at a tracked name — no allowlist needed.
    """
    by_id: dict[int, dict] = {t["id"]: t for t in themes if t.get("id")}
    result: dict[int, str] = {}
    for t in themes:
        tid = t.get("id")
        if not tid:
            continue
        cur: dict | None = t
        seen: set[int] = set()
        while cur and cur.get("id") not in seen:
            seen.add(cur["id"])
            if cur.get("parent_id") is None:
                result[tid] = cur.get("name", "") or ""
                break
            cur = by_id.get(cur.get("parent_id"))
        # ponytail: if cycle or dangling parent, omit — callers default to ""
    return result


def fetch_rb_sets(key: str) -> list[dict]:
    """Fetch ALL Rebrickable sets (~20 k), labeled by top-level theme name.

    Paginates /sets/ with no theme_id filter (all themes, all years).
    ~200 pages × 100 sets at 1 req/s ≈ 3-4 min.
    Defensive: a failed page is logged and skipped, never fatal.
    De-dupes by set_num (first-seen wins).
    """
    themes_raw = _load_themes_raw(key)
    print(f"  resolved {len(themes_raw)} Rebrickable themes", flush=True)
    id_to_root = build_id_to_root_name(themes_raw)

    sets_by_id: dict[str, dict] = {}  # de-dupe by set_num; first-seen wins
    page = 1
    while page <= 300:  # ponytail: safety cap — ~20k sets / 100 = ~200 pages expected
        try:
            data = rb_get("/sets/", key, {"page": page, "page_size": 100})
        except Exception as e:  # noqa: BLE001
            print(f"  WARN: /sets/ page {page} failed: {e}", file=sys.stderr)
            break
        for s in data.get("results", []):
            # ponytail: skip pure gear/promotional items that have zero parts
            # (books, keychains, cardboard displays — not collectible builds)
            if s.get("num_parts", 1) == 0:
                continue
            s["theme_name"] = id_to_root.get(s.get("theme_id"), "")
            sets_by_id.setdefault(s.get("set_num", ""), s)  # de-dupe; first-seen wins
        if not data.get("next"):
            break
        if page % 20 == 0:
            print(f"  ...page {page}, {len(sets_by_id)} sets so far", flush=True)
        page += 1

    print(f"  fetched {len(sets_by_id)} sets across {page} pages", flush=True)
    return list(sets_by_id.values())


def normalize_rb_set(s: dict) -> dict:
    """Map a Rebrickable set dict → partial Trove item dict."""
    raw = s.get("set_num", "")
    set_num = raw.rsplit("-", 1)[0] if "-" in raw else raw  # "10307-1" → "10307"
    return {
        "id": set_num,
        "category": "LEGO",
        "name": s.get("name", ""),
        "imageURL": s.get("set_img_url") or "",
        "themeOrSeries": s.get("theme_name", ""),
        "retailPrice": s.get("retail_price"),  # RB omits price; Brickset fills below (EUR, back-compat)
        "prices": {},                           # native per-currency prices; Brickset fills below
        "lifecycleStatus": "AVAILABLE",        # overridden by Brickset enrichment
        "retirementDate": None,
        "marketPrice": None,
        "year": s.get("year"),                  # RB release year — drives the app's "Newest" sort
        "volumeCount": None,
        "ongoing": None,
    }


# ---------------------------------------------------------------------------
# Brickset helpers
# ---------------------------------------------------------------------------

BS_API = "https://brickset.com/api/v3.asmx/getSets"


def bs_get(key: str, params: dict) -> dict:
    body = urllib.parse.urlencode({
        "apiKey": key,
        "userHash": "",
        "params": json.dumps(params),
    }).encode()
    # ponytail: POST — getSets accepts form-encoded POST and avoids long query URLs
    return _urlopen_json(urllib.request.Request(BS_API, data=body))


def fetch_bs_sets(key: str) -> list[dict]:
    """Pull recent-year sets with extended data so we can read LEGO.com availability.

    Brickset has no direct 'retiring soon' filter — we infer lifecycle from
    LEGOCom.dateLastAvailable. Defensive + logs the API status for diagnosis.
    """
    out: list[dict] = []
    this_year = _dt.date.today().year
    for year in range(this_year - 1, this_year + 1):  # last ~2 years
        page = 1
        while page <= 10:
            try:
                data = bs_get(key, {
                    "year": str(year),
                    "pageSize": 500,
                    "pageNumber": page,
                    "extendedData": 1,
                })
            except Exception as e:  # noqa: BLE001 - non-fatal; RB data still useful
                print(f"  WARN: Brickset year {year} page {page} failed: {e}", file=sys.stderr)
                break
            status = data.get("status")
            if status != "success":
                print(f"  WARN: Brickset year {year}: status={status} "
                      f"msg={data.get('message')}", file=sys.stderr)
                break
            sets = data.get("sets", [])
            out.extend(sets)
            if len(sets) < 500:
                break
            page += 1
    return out


# LEGOCom region → ISO currency (native LEGO local prices, NOT FX conversions).
_REGION_CURRENCY = {"DE": "EUR", "US": "USD", "UK": "GBP"}


def _bs_prices(bs: dict) -> dict[str, float]:
    """Native per-currency retail prices from LEGOCom (e.g. {'EUR':749.99,'USD':799.99}).

    ponytail: each value is LEGO's real local shelf price for that region — no FX,
    no rate source, no staleness. Missing regions are simply absent.
    """
    lego = bs.get("LEGOCom") or {}
    out: dict[str, float] = {}
    for region, code in _REGION_CURRENCY.items():
        price = (lego.get(region) or {}).get("retailPrice")
        if price is not None:
            out[code] = price
    return out


def _bs_eol(bs: dict) -> str | None:
    """dateLastAvailable (yyyy-MM-dd) from any region — currency-agnostic."""
    lego = bs.get("LEGOCom") or {}
    for region in ("DE", "UK", "US"):
        d = (lego.get(region) or {}).get("dateLastAvailable")
        if d:
            return str(d)[:10]
    return None


def bs_lifecycle(bs: dict) -> tuple[str | None, str | None]:
    """Return (lifecycleStatus, retirementDate) from a Brickset set dict.

    Primary signal: LEGOCom.dateLastAvailable (past → RETIRED, future → RETIRING_SOON).
    Falls back to legacy fixture fields so --sample mode keeps working.
    """
    last = _bs_eol(bs)
    if last:
        today = _dt.date.today().isoformat()
        return ("RETIRED", last) if last <= today else ("RETIRING_SOON", last)

    # Legacy fixture shape (offline --sample fixtures predate the LEGOCom parse).
    availability = (bs.get("availability") or "")
    retired_date = bs.get("dateRetired") or bs.get("dateAvailableToDate") or ""
    date_str = retired_date[:10] if retired_date else None
    if "Retired" in availability or bs.get("yearTo"):
        return ("RETIRED", date_str)
    if "retiring" in availability.lower() or "EOFY" in availability:
        return ("RETIRING_SOON", date_str)
    return (None, None)


def _bs_image(bs: dict) -> str:
    img = bs.get("image")
    return img.get("imageURL", "") if isinstance(img, dict) else ""


# ---------------------------------------------------------------------------
# LEGO.com "retiring soon" scrape — forward-looking lifecycle signal.
#
# Brickset's dateLastAvailable (bs_lifecycle above) is retrospective: it only tells
# us a set already left shelves, so RETIRING_SOON never fires before RETIRED already
# would. LEGO.com's own "last-chance-to-buy" category page is the only forward-looking
# signal available without a paid API, so we scrape it.
# ---------------------------------------------------------------------------

# ponytail: "retiring-soon" is a marketing alias that 301s to "last-chance-to-buy" —
# fetch the canonical URL directly rather than following a redirect.
LEGO_RETIRING_URL = "https://www.lego.com/en-us/categories/last-chance-to-buy"
_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def parse_lego_retiring_html(html: str) -> set[str]:
    """Extract LEGO set numbers from a lego.com last-chance-to-buy category page.

    Product listing data isn't in visible HTML — it's Next.js/Apollo cache state
    embedded as `<script id="__NEXT_DATA__">…</script>`. Every product-listing query
    on the page shows up as a top-level `ProductQueryResult:<uuid>` key (nested facet/
    filter sub-keys contain a "." and are skipped) whose `results` list holds entries
    like `{"id": "SingleVariantProduct:10316", ...}`; the set number is the suffix
    after the last colon.

    ponytail: this scrapes an undocumented internal cache shape, verified against a
    real fetch on 2026-07-03 (70 sets, 4 pages). It WILL break silently the day LEGO
    reshuffles their Next.js build or Apollo schema — that's why callers treat a
    zero-result parse as failure, and overrides/retiring.json exists as the manual
    fallback (see load_overrides below).
    """
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return set()
    try:
        apollo = json.loads(m.group(1))["props"]["pageProps"]["__APOLLO_STATE__"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return set()
    nums: set[str] = set()
    for key, val in apollo.items():
        if not key.startswith("ProductQueryResult:") or "." in key:
            continue
        for ref in (val or {}).get("results", []):
            num = str(ref.get("id", "")).rsplit(":", 1)[-1]
            if num.isdigit():
                nums.add(num)
    return nums


def fetch_lego_retiring_soon(max_pages: int = 10) -> set[str]:
    """Page through the last-chance-to-buy category, union-ing set numbers found.

    LEGO.com returns 200 with no ProductQueryResult past the last page (not a 404 or
    empty `results`), so "this page parsed to zero sets" is the natural stop condition.
    A page-fetch failure (network/HTTP error, incl. a 403 from a non-browser UA) just
    stops pagination — whatever was collected on earlier pages is still returned.
    """
    nums: set[str] = set()
    for page in range(1, max_pages + 1):
        url = LEGO_RETIRING_URL + (f"?page={page}" if page > 1 else "")
        req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                html = r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001 - network failure -> stop, keep partial
            print(f"  WARN: LEGO.com retiring-soon page {page} failed: {e}", file=sys.stderr)
            break
        page_nums = parse_lego_retiring_html(html)
        if not page_nums:
            break
        nums |= page_nums
    return nums


def apply_lego_retiring_soon(items: list[dict], set_nums: set[str]) -> int:
    """Promote AVAILABLE items whose id is in set_nums to RETIRING_SOON.

    retirementDate is left as-is (null if unset) — the app handles a null date.
    Never touches RETIRED or already-RETIRING_SOON items. Returns count changed.
    """
    changed = 0
    for item in items:
        if item["id"] in set_nums and item["lifecycleStatus"] == "AVAILABLE":
            item["lifecycleStatus"] = "RETIRING_SOON"
            changed += 1
    return changed


def lego_retiring_step(items: list[dict], set_nums: set[str]) -> int:
    """main()'s entry point for the LEGO.com signal, with the "zero is suspicious"
    guard: whether set_nums is empty because the fetch failed or because a successful
    scrape genuinely found nothing, either way we must never regress existing
    statuses because of a bad scrape — so skip the step entirely and warn loudly.
    """
    if not set_nums:
        print("WARN: LEGO.com retiring-soon scrape yielded 0 sets — skipping "
              "(treating as failure; existing lifecycle statuses left untouched)",
              file=sys.stderr)
        return 0
    changed = apply_lego_retiring_soon(items, set_nums)
    print(f"  LEGO.com retiring-soon: {len(set_nums)} set(s) found, "
          f"{changed} item(s) promoted to RETIRING_SOON")
    return changed


# ---------------------------------------------------------------------------
# Last-known-good cache — survives a single bad scrape across cron runs.
#
# ingest.py rebuilds the catalog from scratch every run, so a scrape failure with
# no fallback silently regresses that day's published RETIRING_SOON statuses back
# to nothing. The cron commits cache/retiring_last_good.json to the data branch,
# so it persists across runs even though the rest of the catalog is rebuilt fresh.
# ---------------------------------------------------------------------------

def load_retiring_cache(path: Path = CACHE_PATH) -> dict:
    """Load {"scrapedAt": iso-str|None, "setNums": [...]}. Missing/malformed file
    -> empty cache, never fatal — this is a fallback, not authoritative data."""
    if not path.exists():
        return {"scrapedAt": None, "setNums": []}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"scrapedAt": None, "setNums": []}
    if not isinstance(data, dict):
        return {"scrapedAt": None, "setNums": []}
    return {"scrapedAt": data.get("scrapedAt"), "setNums": list(data.get("setNums") or [])}


def save_retiring_cache(set_nums: set[str], now: _dt.datetime, path: Path = CACHE_PATH) -> None:
    """Persist a successful scrape as the new last-known-good."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "scrapedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "setNums": sorted(set_nums),
    }, indent=2))


def cache_is_fresh(scraped_at: str | None, now: _dt.datetime,
                    max_age_days: int = CACHE_MAX_AGE_DAYS) -> bool:
    """True if scraped_at parses and is younger than max_age_days."""
    if not scraped_at:
        return False
    try:
        then = _dt.datetime.strptime(scraped_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return False
    return (now - then) < _dt.timedelta(days=max_age_days)


def resolve_lego_retiring_soon(fresh_nums: set[str], now: _dt.datetime,
                                cache_path: Path = CACHE_PATH) -> tuple[set[str], bool, str | None]:
    """Live-mode failsafe wrapping a raw scrape result with the last-known-good cache.

    Returns (set_nums_to_use, scrape_ok, scraped_at):
    - Scrape succeeded (nonzero) -> cache is rewritten with now + these sets; used as-is.
    - Scrape failed/zero -> fall back to the cache if it exists and is younger than
      CACHE_MAX_AGE_DAYS (WARN loudly); otherwise proceed with zero (current behavior).
    """
    if fresh_nums:
        save_retiring_cache(fresh_nums, now, cache_path)
        return fresh_nums, True, now.strftime("%Y-%m-%dT%H:%M:%SZ")

    cache = load_retiring_cache(cache_path)
    if cache_is_fresh(cache["scrapedAt"], now):
        print(f"WARN: LEGO.com scrape failed/yielded 0 sets — using last-known-good "
              f"from {cache['scrapedAt']} ({len(cache['setNums'])} set(s))", file=sys.stderr)
        return set(cache["setNums"]), False, cache["scrapedAt"]

    print(f"WARN: LEGO.com scrape failed/yielded 0 sets and no usable cache (missing or "
          f"older than {CACHE_MAX_AGE_DAYS} days) — proceeding with 0 sets", file=sys.stderr)
    return set(), False, None


# ---------------------------------------------------------------------------
# Curated overrides — manual escape hatch, authoritative over every other signal.
# ---------------------------------------------------------------------------

def load_overrides(path: Path = OVERRIDES_PATH) -> dict[str, str | None]:
    """Load {set_id: "yyyy-MM-dd" | null} from overrides/retiring.json.

    Missing file = skip silently (nothing to override, not an error). Malformed JSON
    or wrong shape = loud failure — a typo here must never silently ship a catalog
    that's missing the overrides.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: {path} is malformed JSON: {e}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print(f"ERROR: {path} must be a JSON object mapping id -> date|null", file=sys.stderr)
        sys.exit(1)
    return data


def apply_overrides(items: list[dict], overrides: dict[str, str | None]) -> int:
    """Merge overrides LAST — authoritative regardless of current status."""
    changed = 0
    for item in items:
        if item["id"] in overrides:
            item["lifecycleStatus"] = "RETIRING_SOON"
            date = overrides[item["id"]]
            if date:
                item["retirementDate"] = date
            changed += 1
    return changed


def overrides_step(items: list[dict], path: Path = OVERRIDES_PATH) -> int:
    """main()'s entry point for the overrides escape hatch."""
    overrides = load_overrides(path)
    if not overrides:
        return 0
    changed = apply_overrides(items, overrides)
    print(f"  overrides/retiring.json: {len(overrides)} entr"
          f"{'y' if len(overrides) == 1 else 'ies'}, {changed} item(s) set to RETIRING_SOON")
    return changed


# ---------------------------------------------------------------------------
# Sample / fixture mode
# ---------------------------------------------------------------------------

def load_fixtures() -> tuple[list[dict], list[dict]]:
    """Load pre-committed fixture files instead of calling live APIs."""
    rb_path = FIXTURES / "rebrickable_sets.json"
    bs_path = FIXTURES / "brickset_retiring.json"
    if not rb_path.exists() or not bs_path.exists():
        print("ERROR: fixtures not found. See README.", file=sys.stderr)
        sys.exit(1)
    return json.loads(rb_path.read_text()), json.loads(bs_path.read_text())


def load_lego_fixture() -> set[str]:
    """--sample mode equivalent of fetch_lego_retiring_soon(): parse the committed
    offline HTML fixture instead of hitting the network. Missing fixture -> empty
    set (same "treat as failure, skip" path as a real failed fetch)."""
    path = FIXTURES / "lego_retiring_soon.html"
    if not path.exists():
        return set()
    return parse_lego_retiring_html(path.read_text())


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------

# ponytail: Brickset only prices/classifies ~recent sets, so the ~17k Rebrickable-only
# sets sit at AVAILABLE forever even when they've long since left shelves. Infer RETIRED
# from age — no current price + released >= this many years ago => off-shelf. Conservative
# window so we never retire a still-current set. Tune if LEGO lifespans shift.
RETIRE_AGE_YEARS = 3


def infer_retired(status: str, has_price: bool, year: int | None, now_year: int) -> str:
    """Age-based retirement for sets Brickset didn't classify. See RETIRE_AGE_YEARS."""
    if (status == "AVAILABLE" and not has_price
            and isinstance(year, int) and year <= now_year - RETIRE_AGE_YEARS):
        return "RETIRED"
    return status


def merge(rb_sets: list[dict], bs_sets: list[dict]) -> list[dict]:
    """Merge Rebrickable base data with Brickset lifecycle enrichment."""
    bs_index: dict[str, dict] = {}
    for s in bs_sets:
        num = str(s.get("number", ""))
        if num:
            bs_index[num] = s

    items: list[dict] = []
    seen: set[str] = set()

    for s in rb_sets:
        item = normalize_rb_set(s)
        set_id = item["id"]
        if not set_id or set_id in seen:
            continue
        seen.add(set_id)

        bs = bs_index.get(set_id)
        if bs:
            status, date = bs_lifecycle(bs)
            if status:
                item["lifecycleStatus"] = status
            if date:
                item["retirementDate"] = date
            prices = _bs_prices(bs)
            if prices:
                item["prices"] = prices
                if item["retailPrice"] is None:
                    item["retailPrice"] = prices.get("EUR")  # back-compat single price
        item["lifecycleStatus"] = infer_retired(
            item["lifecycleStatus"],
            bool(item["prices"]) or item["retailPrice"] is not None,
            s.get("year"),
            _dt.date.today().year,
        )
        items.append(item)

    # Add Brickset-only retiring/retired sets not in our Rebrickable pull.
    for num, bs in bs_index.items():
        if num in seen:
            continue
        status, date = bs_lifecycle(bs)
        if status in ("RETIRING_SOON", "RETIRED"):
            prices = _bs_prices(bs)
            items.append({
                "id": num,
                "category": "LEGO",
                "name": bs.get("name", ""),
                "imageURL": _bs_image(bs),
                "themeOrSeries": bs.get("theme", ""),
                "retailPrice": prices.get("EUR"),
                "prices": prices,
                "lifecycleStatus": status,
                "retirementDate": date,
                "marketPrice": None,
                "year": bs.get("year"),  # Brickset release year for RB-absent retiring/retired sets
                "volumeCount": None,
                "ongoing": None,
            })

    return items


def build_catalog(items: list[dict]) -> dict:
    return {
        "version": VERSION,
        "currency": "EUR",  # all retailPrice values are euros (Brickset DE region)
        "_attribution": "LEGO data: Rebrickable (rebrickable.com) + Brickset "
                        "(brickset.com). Not affiliated with or endorsed by The LEGO Group.",
        "_generated": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "items": items,
    }


def build_status(scrape_ok: bool, scraped_at: str | None, retiring_count: int,
                  degraded: bool) -> dict:
    """Machine-readable run signal written alongside catalog.json.

    `degraded` is true when the run fell back to the last-known-good cache, or
    ended with zero RETIRING_SOON items from every source — the workflow reads
    this AFTER publishing to turn a cron run red without blocking the (still
    valid) catalog from shipping.
    """
    return {
        "generatedAt": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scrapeOK": scrape_ok,
        "scrapedAt": scraped_at,
        "retiringSoonCount": retiring_count,
        "degraded": degraded,
    }


def write_status(scrape_ok: bool, scraped_at: str | None, retiring_count: int,
                  degraded: bool, path: Path = STATUS_PATH) -> None:
    path.write_text(json.dumps(
        build_status(scrape_ok, scraped_at, retiring_count, degraded), indent=2))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    sample_mode = "--sample" in sys.argv

    if sample_mode:
        print("Running in --sample mode (fixtures only, no API calls)")
        rb_sets, bs_sets = load_fixtures()
    else:
        rb_key = os.environ.get("REBRICKABLE_API_KEY", "")
        bs_key = os.environ.get("BRICKSET_API_KEY", "")
        if not rb_key or not bs_key:
            print("ERROR: REBRICKABLE_API_KEY and BRICKSET_API_KEY must be set "
                  "(or pass --sample).", file=sys.stderr)
            sys.exit(1)

        print("Fetching Rebrickable sets…")
        rb_sets = fetch_rb_sets(rb_key)
        print(f"  {len(rb_sets)} sets from Rebrickable")

        print("Fetching Brickset (lifecycle enrichment)…")
        try:
            bs_sets = fetch_bs_sets(bs_key)
        except Exception as e:  # noqa: BLE001 - Brickset failure must not lose RB data
            print(f"  WARN: Brickset fetch failed entirely: {e}", file=sys.stderr)
            bs_sets = []
        print(f"  {len(bs_sets)} sets from Brickset")

    items = merge(rb_sets, bs_sets)

    if not sample_mode and len(items) < MIN_ITEMS:
        print(f"ERROR: only {len(items)} items produced — refusing to overwrite the "
              f"published catalog (floor is {MIN_ITEMS}).", file=sys.stderr)
        sys.exit(1)

    # A: LEGO.com retiring-soon scrape — the only forward-looking lifecycle signal.
    now = _dt.datetime.now(_dt.timezone.utc)
    if sample_mode:
        lego_nums = load_lego_fixture()
        scrape_ok = bool(lego_nums)
        used_cache = False
        scraped_at = now.strftime("%Y-%m-%dT%H:%M:%SZ") if scrape_ok else None
    else:
        print("Fetching LEGO.com retiring-soon…")
        fresh_nums = fetch_lego_retiring_soon()
        lego_nums, scrape_ok, scraped_at = resolve_lego_retiring_soon(fresh_nums, now)
        used_cache = not scrape_ok and bool(lego_nums)
    lego_retiring_step(items, lego_nums)

    # B: curated overrides — manual escape hatch, merged last, authoritative.
    overrides_step(items)

    avail = sum(1 for i in items if i["lifecycleStatus"] == "AVAILABLE")
    soon = sum(1 for i in items if i["lifecycleStatus"] == "RETIRING_SOON")
    retired = sum(1 for i in items if i["lifecycleStatus"] == "RETIRED")
    print(f"Lifecycle: AVAILABLE={avail} RETIRING_SOON={soon} RETIRED={retired}",
          file=sys.stderr)

    catalog = build_catalog(items)
    OUT.write_text(json.dumps(catalog, indent=2, default=str))
    print(f"Wrote {len(items)} items ({soon + retired} with retirement status) → {OUT}")

    degraded = used_cache or soon == 0
    write_status(scrape_ok, scraped_at, soon, degraded)
    if degraded:
        print(f"WARN: run is DEGRADED (used_cache={used_cache}, retiringSoonCount={soon}) "
              f"— see {STATUS_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
