# Trove catalog.json schema

## Envelope

```json
{
  "version": 2,
  "_attribution": "…",
  "_generated": "2026-06-19T03:00:00Z",
  "items": [...]
}
```

| Field | Type | Notes |
|---|---|---|
| `version` | `Int` | Bumped on breaking schema changes |
| `_attribution` | `String` | Human-readable source credit; ignored by decoder |
| `_generated` | `String` | ISO-8601 UTC timestamp; ignored by decoder |
| `items` | `[CatalogItem]` | Array of items (see below) |

## Item fields

| JSON field | Swift type | Source | Notes |
|---|---|---|---|
| `id` | `String` | Rebrickable `set_num` (suffix `-1` stripped) | LEGO set number, e.g. `"10307"` |
| `category` | `String` | Hardcoded `"LEGO"` for pipeline output | Matches `CollectorCategory.lego` raw value |
| `name` | `String` | Rebrickable `name` | Display name |
| `imageURL` | `String` | Rebrickable `set_img_url` | CDN URL to box render; may be `""` |
| `themeOrSeries` | `String` | Rebrickable `theme_name` | e.g. `"Architecture"`, `"Icons"`, `"Ideas"` |
| `retailPrice` | `Number?` | Rebrickable `retail_price` / Brickset fallback | EUR/USD; `null` if unknown |
| `lifecycleStatus` | `String?` | Brickset `availability` → normalized | `"AVAILABLE"` / `"RETIRING_SOON"` / `"RETIRED"` |
| `retirementDate` | `String?` | Brickset `dateAvailableToDate` or `dateRetired` | `"yyyy-MM-dd"` or `null` |
| `marketPrice` | `Number?` | Reserved for BrickLink (not yet wired) | `null` in current output |
| `volumeCount` | `Number?` | Manga only | `null` for LEGO |
| `ongoing` | `Boolean?` | Manga only | `null` for LEGO |

## Lifecycle mapping

| Brickset `availability` | Trove `lifecycleStatus` |
|---|---|
| `"Retiring Soon"` / `"EOFY"` / contains "retiring" | `RETIRING_SOON` |
| `"Retired"` / `yearTo` set | `RETIRED` |
| anything else | `AVAILABLE` |

## Attribution

LEGO set data sourced from:
- **Rebrickable** (https://rebrickable.com) — set catalog, retail prices, images. Free API key required. Terms: https://rebrickable.com/api/
- **Brickset** (https://brickset.com) — lifecycle/retirement status, retirement dates. Free API key required. Terms: https://brickset.com/api/v3

This project is not affiliated with or endorsed by The LEGO Group, Rebrickable, or Brickset.
