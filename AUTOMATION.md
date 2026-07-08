# Automating `latest_inday_pagasa.json` from PAGASA

This adds a GitHub Action that reads the **official PAGASA Tropical Cyclone Bulletin**,
regenerates `latest_inday_pagasa.json`, and commits it automatically — so you never
edit the file by hand. The dashboard then picks up the new file within 5 minutes.

## Files (drop them into your repo, keeping these paths)

```
your-repo/
├── CPF_INDAY_Satellite_Dashboard.html
├── latest_inday_pagasa.json           # updated automatically from now on
├── scripts/
│   └── update_pagasa.py               # the scraper (Python standard library only)
└── .github/
    └── workflows/
        └── update-pagasa.yml          # runs every 30 min
```

## One-time setup (2 minutes)

1. Commit the three files above to your repo.
2. In GitHub: **Settings → Actions → General → Workflow permissions →** choose
   **“Read and write permissions”** → Save. (Lets the bot commit the JSON.)
3. Open the **Actions** tab → select **“Update PAGASA bulletin”** → **Run workflow**
   once to confirm it works. After that it runs on its own every ~30 minutes.

That’s it. When PAGASA issues a new bulletin, the bot commits the new JSON and the
dashboard flips to it automatically (or tap **Check now**).

## What it updates vs preserves (accuracy by design)

**Auto-updated live from the bulletin**
- Current position text **and exact eye lat/long** when PAGASA prints it
  (e.g. `17.1 °N, 132.7 °E` → plotted exactly, `latlonExact:true`)
- Max winds, gusts, central pressure, movement, wind extent, category, name
- Bulletin number + issue time + next-bulletin time
- Highest possible signal, Gale Warning text, wave-height/seaboard list, gust areas

**Preserved / curated** (separate PAGASA products or interpretation — not auto-derived)
- Forecast waypoint coordinates (PAGASA prints these as distances-from-landmarks,
  not decimals; keep editing occasionally if you want the track dots to move)
- Rainfall / habagat 3-day outlook, satellite notes, CPF hubs, risk matrix

**Safety behaviour**
- If the storm isn’t in the current bulletin (exited PAR / no active cyclone) or the
  core fields can’t be parsed, the script **does not overwrite** the file — the
  dashboard keeps the last verified data.
- It commits **only when the bulletin actually changes** (no timestamp-only churn).
- Values PAGASA doesn’t state become `"Not specified in latest PAGASA bulletin"`.

## Switching to the next storm

Edit one line in `.github/workflows/update-pagasa.yml`:

```yaml
STORM_NAME: INDAY     # -> e.g. HENRY
```

(If you also rename the JSON, update `OUT_PATH` here **and** the filename the
dashboard fetches.)

## Test it locally

```bash
python scripts/update_pagasa.py --dry-run           # print result, don't write
python scripts/update_pagasa.py --from-file page.html   # parse a saved page
```

## Notes / limits

- The scraper uses only Python’s standard library — no `pip install`, no secrets.
- GitHub cron runs in **UTC** and can be delayed a few minutes under load, so the real
  cadence is roughly every 30–60 min. Scheduled workflows also auto-pause after ~60
  days of no repo activity (any push re-enables them).
- It parses PAGASA’s server-rendered HTML. If PAGASA ever switches that page to
  JavaScript-only rendering, the plain fetch would return no bulletin text and the
  script safely keeps the last file — tell me and I’ll ship a headless-browser
  (Playwright) variant of the fetch step.
- PAGASA remains the source of truth; satellite imagery stays visualization-only.
