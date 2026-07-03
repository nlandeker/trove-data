# trove-data

Serverless ingest pipeline for the [Trove iOS app](https://github.com/nejclandeker/trove_app_ios).

A GitHub Actions cron job pulls LEGO set data from [Rebrickable](https://rebrickable.com) and
retirement status from [Brickset](https://brickset.com), normalizes it to the Trove
`CatalogItem` JSON schema, and commits `data/catalog.json` to the `data` branch daily.

The app fetches this static file over HTTPS — no server, no database.

## Files

| File | Purpose |
|---|---|
| `ingest.py` | Ingest + normalize script (stdlib only, no dependencies) |
| `catalog.json` | Sample output for local dev / schema reference |
| `fixtures/` | Offline fixture data for `--sample` mode |
| `overrides/retiring.json` | Manual `RETIRING_SOON` escape hatch — see SCHEMA.md |
| `cache/retiring_last_good.json` | Last-known-good LEGO.com scrape (failsafe, see DEPLOY.md) |
| `status.json` | Per-run health signal (`degraded` flag, see DEPLOY.md) |
| `SCHEMA.md` | JSON schema documentation |
| `.github/workflows/ingest.yml` | Daily cron Action |
| `DEPLOY.md` | Step-by-step setup guide |

## Quick start (sample mode, no API keys)

```bash
python3 ingest.py --sample
```

## Schema

See [SCHEMA.md](SCHEMA.md) for the full `catalog.json` field reference.

## Deploy

See [DEPLOY.md](DEPLOY.md) for the full setup guide (push repo, add secrets, enable Action).

The daily cron also writes `cache/retiring_last_good.json` (last-known-good LEGO.com
scrape, reused if a run's scrape fails or goes stale) and `status.json` (a
`degraded` flag the workflow checks after publish to turn a bad run red without
blocking that day's catalog). See the "Failsafe" section of
[DEPLOY.md](DEPLOY.md) for the exact workflow steps to wire this up.
