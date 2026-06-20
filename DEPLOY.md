# Deploy guide — trove-data repo

## Overview

This directory **is** the `trove-data` public repo (it has its own `.git`).
The app repo (`trove_app_ios`) gitignores it — it lives and is pushed independently.

The ingest script runs daily via GitHub Actions and commits `data/catalog.json` to
the `data` branch, which the app fetches at the placeholder URL below.

## Placeholder URL (used by A2)

```
https://raw.githubusercontent.com/<YOUR_GITHUB_USERNAME>/trove-data/data/data/catalog.json
```

Replace `<YOUR_GITHUB_USERNAME>` with your GitHub handle (e.g. `nejclandeker`).
Store this constant in the app as `TroveConfig.catalogURL` (wired up in A2).

## Step-by-step

### 1. Create the public repo on GitHub

1. Go to https://github.com/new
2. Name: `trove-data`
3. Visibility: **Public** (required for raw.githubusercontent.com access without auth)
4. **Do NOT** initialize with a README (this repo already has one)
5. Click **Create repository**

### 2. Push this repo

```bash
cd /path/to/trove_app_ios/data-pipeline   # wherever this dir lives locally

git remote add origin https://github.com/<YOUR_GITHUB_USERNAME>/trove-data.git
git push -u origin main

# Create the data branch (where catalog.json will be committed by the Action)
git checkout -b data
mkdir -p data
cp catalog.json data/catalog.json
git add data/catalog.json
git commit -m "chore: seed data branch with sample catalog"
git push -u origin data

git checkout main   # return to main
```

### 3. Add API key secrets

In the `trove-data` GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret name | Where to get it |
|---|---|
| `REBRICKABLE_API_KEY` | https://rebrickable.com/api/ (free account → profile) |
| `BRICKSET_API_KEY` | https://brickset.com/tools/webservices/requestkey (instant email) |

### 4. Enable the workflow

The workflow file is at `.github/workflows/ingest.yml`.
After pushing to `main`, go to **Actions → Ingest catalog → Run workflow** to trigger a manual run.

On success the script commits `data/catalog.json` to the `data` branch.

### 5. The data URL

Once the `data` branch has `data/catalog.json`, it is immediately accessible at:

```
https://raw.githubusercontent.com/<YOUR_GITHUB_USERNAME>/trove-data/data/data/catalog.json
```

No GitHub Pages setup needed — `raw.githubusercontent.com` serves the file directly.

## Verify

```bash
curl -s "https://raw.githubusercontent.com/<YOUR_GITHUB_USERNAME>/trove-data/data/data/catalog.json" | \
  python3 -c "import json,sys; d=json.load(sys.stdin); print(d['version'], len(d['items']), 'items')"
```

## ponytail notes

- No GitHub Pages required — raw.githubusercontent.com is simpler.
- The `data` branch doubles as the "CDN"; no extra infrastructure.
- Add gzip: the Action can `gzip -k data/catalog.json` and commit `catalog.json.gz` if payload grows large; the app would need `Accept-Encoding: gzip` header handling (defer to A2).
- The app-repo fixture at `Packages/TroveCore/Tests/TroveCoreTests/Fixtures/sample_catalog.json` mirrors this sample output and is the offline test input for A2's stub tests.
