# niowaste

The Bavarian LGL publishes weekly SARS-CoV-2 virus activity in wastewater at
[bay-voc.lgl.bayern.de/abwassermonitoring](https://bay-voc.lgl.bayern.de/abwassermonitoring),
including a station for **München** — but with no RSS feed and no way to be notified.

This repo polls the dashboard once a day, records every new Munich reading, and turns it into
the two things the site is missing: **an Atom feed** and **an email**.

## How it works

The dashboard is a JSF/PrimeFaces app, but it is fed by an unauthenticated JSON endpoint that
needs no cookies or session:

```
https://bay-voc.lgl.bayern.de/api/v2/frontend/map/sewage/marker?pathogen=SARSCOV2&mode=wval
https://bay-voc.lgl.bayern.de/api/v2/frontend/map/sewage/marker?pathogen=SARSCOV2&mode=trend
```

`mode=wval` returns one record per location:

```json
{"property":{"name":"München","wval":0.00932,"wvalCategory":"Sehr niedrig",
  "sampleDates":["2026-08-31"],"standIsoWeek":36,"trendWeek":"31.08.2026"}}
```

`mode=trend` adds `{"trend":"leicht ansteigend","percentageChange":"+6,51 %"}`.
`pathogen=` also accepts `INFLUENZA` and `RSV`.

So detecting new data is exact — when `sampleDates` advances, there is a new measurement. No
HTML diffing, no false positives from chart re-renders or cookie banners.

```
check.py           the whole thing — Python 3 standard library only, no dependencies
data/*.csv         append-only history; the single source of truth
docs/feed.xml      Atom feed, regenerated from the CSV        ─┐ published via
docs/index.html    small readable page, regenerated from the CSV ┘ GitHub Pages
```

`docs/` is always regenerated from the CSV, never patched in place, so the feed is
reproducible. Entry IDs are derived from the sample date, so re-generating the feed never
re-notifies your reader.

## Notifications

- **Atom feed** — subscribe to `https://<owner>.github.io/niowaste/feed.xml` in NetNewsWire,
  Reeder, or any reader, on both macOS and iOS.
- **Email** — the workflow opens a GitHub issue, and GitHub's own notification email delivers
  it. No SMTP server, no app password, no secrets. Make sure the repo is set to
  **Watch → All Activity** and that email notifications are enabled in your GitHub settings.

  To send real email from an address of your own instead, replace the `gh issue create` step
  with [`dawidd6/action-send-mail`](https://github.com/dawidd6/action-send-mail) and store the
  SMTP credentials as repository secrets.

## Running it locally

```bash
python3 check.py --dry-run    # fetch and report; write nothing
python3 check.py              # record a new reading if there is one
python3 check.py --force      # treat the current reading as new (exercises the write path)
python3 check.py --pathogen INFLUENZA     # or RSV
python3 check.py --location Freising      # any of the 21 Bavarian stations
```

`--endpoint <url>` points the script at a different URL, which is how the failure path gets
tested.

## When the endpoint changes

This is an internal frontend API, not a documented public one. It can change shape without
warning. `check.py` therefore validates the response hard — the list shape, the presence of the
München record, and every field it reads — and **exits non-zero with a specific message**
rather than quietly reporting "no new data" forever:

```
ERROR: the LGL endpoint no longer looks the way this script expects.
  no location named 'München' in ...?pathogen=SARSCOV2&mode=wval. Available: Muenchen, ...
```

A failed run makes GitHub email you the failure, which is the alarm that the API moved. The
`mode=trend` call is deliberately treated as optional: if only the trend breaks, you still get
notified about the new measurement, with a warning on stderr.

## Known caveat

GitHub disables scheduled workflows in public repos after 60 days of no repository activity,
emailing the owner first. The workflow's own commits count as activity, so in practice this
should not fire — but if the LGL publishes nothing for two months, expect that email and
re-enable with one click.

The repo is public because GitHub Pages on a free account requires it. The data is already
public, so nothing sensitive is exposed.

## Data

Wastewater data © Bayerisches Landesamt für Gesundheit und Lebensmittelsicherheit (LGL).
This repo only mirrors the Munich values it reads from the public dashboard.
