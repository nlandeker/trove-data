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

## Failsafe: last-known-good cache + status.json

`ingest.py` rebuilds the catalog from scratch every run. If the LEGO.com
retiring-soon scrape has a bad day, three things now protect that day's publish:

1. **`cache/retiring_last_good.json`** — on a successful scrape, ingest.py
   rewrites this file with the current timestamp + set list. On a failed/zero
   scrape it reads this file back and reuses it as the scrape result, as long
   as it's younger than 21 days (otherwise it WARNs and proceeds with zero,
   same as before this failsafe existed).
2. **`status.json`** — written next to `catalog.json` every run:
   `{"generatedAt", "scrapeOK", "scrapedAt", "retiringSoonCount", "degraded"}`.
   `degraded` is true if the run fell back to the cache, or ended with zero
   RETIRING_SOON items from every source (scrape + overrides combined).
3. A workflow step (below) reads `status.json` **after** the catalog is
   already committed and pushed, and fails the Action run if `degraded` is
   true — so the (still valid) catalog ships, but the cron shows red and
   GitHub's built-in failure-notification email fires.

For this to work across runs, the workflow must commit `cache/retiring_last_good.json`
(alongside `catalog.json` and `status.json`) to the `data` branch — it's the
only thing that survives between "rebuild everything from scratch" runs.

These steps are already wired into `.github/workflows/ingest.yml` (this repo).
For reference:

```yaml
      # (a) Same step as before, but now also stages status.json and the cache file.
      - name: Commit updated catalog
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          mkdir -p data
          mv catalog.json data/catalog.json
          mv status.json data/status.json
          # cache/retiring_last_good.json stays at repo root — ingest.py reads it
          # from there (CACHE_PATH) next run; committing it in place is what makes
          # it survive across from-scratch rebuilds.
          git add data/catalog.json data/status.json cache/retiring_last_good.json
          git diff --cached --quiet || git commit -m "chore: refresh catalog $(date -u +%Y-%m-%d)"
          git push
```

```yaml
      # (b) AFTER publish: turn the run red on a degraded catalog without blocking the push above.
      - name: Fail run if degraded (catalog already published — this just signals red + triggers GH's failure email)
        run: python3 -c "import json,sys; sys.exit(1 if json.load(open('data/status.json'))['degraded'] else 0)"
```

```yaml
      # (c) OPTIONAL: file a tracking issue on degraded runs, guarded so it doesn't spam
      # one issue per day. Uncomment to enable (needs `issues: write` permission added
      # to the job's `permissions:` block, and a `scrape-degraded` label to exist).
      # - name: Open issue on degraded run (dedup via label)
      #   if: always()
      #   env:
      #     GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      #   run: |
      #     DEGRADED=$(python3 -c "import json; print(json.load(open('data/status.json'))['degraded'])")
      #     if [ "$DEGRADED" = "True" ]; then
      #       OPEN=$(gh issue list --repo "$GITHUB_REPOSITORY" --label scrape-degraded --state open --json number --jq length)
      #       if [ "$OPEN" = "0" ]; then
      #         gh issue create --repo "$GITHUB_REPOSITORY" --title "LEGO.com retiring-soon scrape degraded" \
      #           --label scrape-degraded \
      #           --body "Run $(date -u +%Y-%m-%d) is degraded — see data/status.json."
      #       fi
      #     fi
```

## ponytail notes

- No GitHub Pages required — raw.githubusercontent.com is simpler.
- The `data` branch doubles as the "CDN"; no extra infrastructure.
- Add gzip: the Action can `gzip -k data/catalog.json` and commit `catalog.json.gz` if payload grows large; the app would need `Accept-Encoding: gzip` header handling (defer to A2).
- The app-repo fixture at `Packages/TroveCore/Tests/TroveCoreTests/Fixtures/sample_catalog.json` mirrors this sample output and is the offline test input for A2's stub tests.
