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

    retiring = sum(1 for i in items if i["lifecycleStatus"] in ("RETIRING_SOON", "RETIRED"))
    catalog = build_catalog(items)
    OUT.write_text(json.dumps(catalog, indent=2, default=str))
    print(f"Wrote {len(items)} items ({retiring} with retirement status) → {OUT}")


if __name__ == "__main__":
    main()
