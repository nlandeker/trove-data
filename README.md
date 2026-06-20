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
