#!/usr/bin/env python3
"""Build a manga volume-count map: AniList popularity → volume count.

AniList already reports `volumes` for FINISHED series; for ONGOING series it
returns null, so we read the Wikipedia infobox `| volumes = N` field (which
editors keep current as new volumes release). Output: `manga_volumes.json`,
keyed by the same `anilist-{id}` the app already uses for manga CatalogItems.

Series we can't resolve a count for are simply omitted — the app falls back to
its manual volume-count entry.

Run: python3 manga_ingest.py   (env: MANGA_TARGET overrides the top-N, default 500)
"""
import datetime as _dt
import json
import os
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request

ANILIST = "https://graphql.anilist.co"
WIKI = "https://en.wikipedia.org/w/api.php"
UA = {"User-Agent": "TroveMangaIngest/1.0 (https://github.com/nlandeker/trove-data)"}

TARGET = int(os.environ.get("MANGA_TARGET", "500"))
PER_PAGE = 50
OUT = pathlib.Path(__file__).parent / "manga_volumes.json"

POP_QUERY = """
query ($page: Int!, $perPage: Int!) {
  Page(page: $page, perPage: $perPage) {
    media(type: MANGA, isAdult: false, sort: POPULARITY_DESC) {
      id
      title { english romaji }
      volumes
      status
    }
  }
}
"""


# --- AniList ---------------------------------------------------------------

def anilist_popular(target: int) -> list[dict]:
    """Top-`target` manga by AniList popularity (paginated, polite)."""
    out: list[dict] = []
    page = 1
    while len(out) < target:
        body = json.dumps({"query": POP_QUERY,
                           "variables": {"page": page, "perPage": PER_PAGE}}).encode()
        req = urllib.request.Request(
            ANILIST, data=body,
            headers={**UA, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            d = json.load(urllib.request.urlopen(req, timeout=20))
        except Exception as e:  # noqa: BLE001 - non-fatal; stop paginating
            print(f"WARN: AniList page {page}: {e}", file=sys.stderr)
            break
        media = d.get("data", {}).get("Page", {}).get("media", [])
        if not media:
            break
        out.extend(media)
        page += 1
        time.sleep(0.7)  # AniList rate limit ~90 req/min
    return out[:target]


# --- Wikipedia -------------------------------------------------------------

def _wiki_get(params: dict) -> dict:
    url = WIKI + "?" + urllib.parse.urlencode(params)
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15))


def _norm(s: str) -> set:
    return set(re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).split())


def title_matches(query: str, article: str) -> bool:
    """Guard against confident-but-wrong matches: require decent token overlap."""
    q = _norm(query)
    if not q:
        return False
    return len(q & _norm(article)) / len(q) >= 0.5


def parse_infobox_volumes(wikitext: str | None) -> int | None:
    """Read `| volumes = N` from the animanga infobox. None if absent."""
    if not wikitext:
        return None
    m = re.search(r"\|\s*volumes\s*=\s*([0-9]{1,4})", wikitext, re.IGNORECASE)
    return int(m.group(1)) if m else None


def wiki_volume_count(title: str) -> int | None:
    """Best-effort Wikipedia volume count for a manga title, or None."""
    try:
        d = _wiki_get({"action": "query", "list": "search",
                       "srsearch": f"{title} manga", "srlimit": 1, "format": "json"})
        hits = d.get("query", {}).get("search", [])
        if not hits:
            return None
        article = hits[0]["title"]
        if not title_matches(title, article):
            return None  # wrong article — don't publish a confident-but-wrong count
        d2 = _wiki_get({"action": "query", "prop": "revisions", "titles": article,
                        "rvprop": "content", "rvslots": "main", "format": "json"})
        for p in d2.get("query", {}).get("pages", {}).values():
            try:
                return parse_infobox_volumes(p["revisions"][0]["slots"]["main"]["*"])
            except (KeyError, IndexError):
                return None
    except Exception as e:  # noqa: BLE001 - non-fatal; omit this series
        print(f"WARN: Wikipedia '{title}': {e}", file=sys.stderr)
    return None


# --- Main ------------------------------------------------------------------

def build(manga: list[dict]) -> list[dict]:
    items, wiki_calls = [], 0
    for i, m in enumerate(manga):
        title = m["title"].get("english") or m["title"].get("romaji") or ""
        if not title:
            continue
        vol = m.get("volumes")          # AniList: total for finished, null for ongoing
        if vol is None:                 # ongoing → editor-maintained Wikipedia count
            vol = wiki_volume_count(title)
            wiki_calls += 1
            time.sleep(0.5)             # polite to Wikipedia
        if not vol:
            continue                    # unknown → app falls back to manual entry
        items.append({
            "id": f"anilist-{m['id']}",
            "name": title,
            "volumeCount": vol,
            "ongoing": m.get("status") == "RELEASING",
        })
        if (i + 1) % 50 == 0:
            print(f"  ...{i + 1} processed, {len(items)} with counts", flush=True)
    print(f"  {wiki_calls} Wikipedia lookups for ongoing series", flush=True)
    return items


def main() -> None:
    manga = anilist_popular(TARGET)
    print(f"fetched {len(manga)} popular manga from AniList", flush=True)
    items = build(manga)
    OUT.write_text(json.dumps({
        "version": 1,
        "_generated": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "items": items,
    }, ensure_ascii=False, indent=2))
    print(f"wrote {len(items)} manga volume counts → {OUT.name}")


if __name__ == "__main__":
    main()
