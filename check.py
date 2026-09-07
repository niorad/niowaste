#!/usr/bin/env python3
"""Watch the Bavarian LGL wastewater dashboard for new Munich readings.

https://bay-voc.lgl.bayern.de/abwassermonitoring has no RSS feed and no
notifications, but the JSF frontend is fed by an unauthenticated JSON endpoint
that needs no cookies or session. This script polls that endpoint, keeps an
append-only CSV history, and regenerates an Atom feed plus a small HTML page
from that history.

That endpoint is an internal frontend API, not a documented public one. If its
shape changes, this script exits non-zero with an explanation rather than
silently reporting "no new data" forever.
"""

import argparse
import csv
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

try:
    from zoneinfo import ZoneInfo

    BERLIN = ZoneInfo("Europe/Berlin")
except Exception:  # pragma: no cover - only if tzdata is unavailable
    BERLIN = timezone.utc

API_BASE = "https://bay-voc.lgl.bayern.de/api/v2/frontend/map/sewage/marker"
DASHBOARD = "https://bay-voc.lgl.bayern.de/abwassermonitoring"
FEED_AUTHORITY = "bay-voc.lgl.bayern.de"
USER_AGENT = (
    "niowaste/1.0 (personal wastewater-monitoring notifier; "
    "+https://github.com/niowaste)"
)
ATOM = "http://www.w3.org/2005/Atom"
FIELDNAMES = [
    "sample_date",
    "iso_week",
    "wval",
    "category",
    "trend",
    "percent_change",
    "fetched_at",
]
MAX_FEED_ENTRIES = 50

ROOT = Path(__file__).resolve().parent


class EndpointError(Exception):
    """The upstream endpoint is unreachable or no longer has the shape we expect."""


# --------------------------------------------------------------------------- fetch


def fetch_json(url, timeout=30, retries=2):
    last = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(3 * attempt)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            break
        except (urllib.error.URLError, OSError) as exc:
            last = exc
    else:
        raise EndpointError(f"could not reach {url} after {retries + 1} attempts: {last}")

    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        preview = raw[:200].decode("utf-8", "replace")
        raise EndpointError(
            f"{url} did not return JSON ({exc}). First bytes: {preview!r}"
        ) from exc


def location_property(payload, location, url):
    """Pull one location's `property` dict out of the marker response."""
    if not isinstance(payload, list) or not payload:
        raise EndpointError(f"{url} returned {type(payload).__name__}, expected a non-empty list")

    names = []
    for feature in payload:
        if not isinstance(feature, dict):
            continue
        prop = feature.get("property")
        if not isinstance(prop, dict) or "name" not in prop:
            continue
        names.append(prop["name"])
        if prop["name"] == location:
            return prop

    if not names:
        raise EndpointError(
            f"{url} returned a list, but no entry had a 'property.name' field. "
            "The response format changed."
        )
    raise EndpointError(
        f"no location named {location!r} in {url}. Available: {', '.join(sorted(names))}"
    )


def validate_wval(prop, url):
    missing = [k for k in ("wval", "wvalCategory", "sampleDates", "standIsoWeek") if k not in prop]
    if missing:
        raise EndpointError(
            f"{url}: the location record is missing {', '.join(missing)}. "
            f"Got keys: {', '.join(sorted(prop))}"
        )
    if not isinstance(prop["wval"], (int, float)):
        raise EndpointError(f"{url}: 'wval' is {prop['wval']!r}, expected a number")
    dates = prop["sampleDates"]
    if not isinstance(dates, list) or not dates or not all(isinstance(d, str) for d in dates):
        raise EndpointError(f"{url}: 'sampleDates' is {dates!r}, expected a non-empty list of strings")
    return prop


def current_reading(pathogen, location, endpoint):
    """Fetch the latest reading. The wval call is authoritative; trend is a nice-to-have."""
    wval_url = f"{endpoint}?pathogen={pathogen}&mode=wval"
    prop = validate_wval(location_property(fetch_json(wval_url), location, wval_url), wval_url)

    trend, percent_change = "", ""
    trend_url = f"{endpoint}?pathogen={pathogen}&mode=trend"
    try:
        tprop = location_property(fetch_json(trend_url), location, trend_url)
        trend = str(tprop.get("trend", "") or "")
        percent_change = str(tprop.get("percentageChange", "") or "")
    except EndpointError as exc:
        # Losing the trend must not block a notification about genuinely new data.
        print(f"warning: trend unavailable, continuing without it ({exc})", file=sys.stderr)

    return {
        "sample_date": prop["sampleDates"][-1],
        "iso_week": str(prop["standIsoWeek"]),
        "wval": repr(float(prop["wval"])),
        "category": str(prop["wvalCategory"]),
        "trend": trend,
        "percent_change": percent_change,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------- history


def read_history(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        rows = [dict(r) for r in csv.DictReader(fh)]
    return sorted(rows, key=lambda r: r["sample_date"])


def write_history(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: r["sample_date"]))


def upsert(rows, row):
    """Replace the row for this sample_date if present, else append. Keeps --force idempotent."""
    for i, existing in enumerate(rows):
        if existing["sample_date"] == row["sample_date"]:
            rows[i] = row
            return sorted(rows, key=lambda r: r["sample_date"])
    return sorted(rows + [row], key=lambda r: r["sample_date"])


# --------------------------------------------------------------------------- rendering


def de_number(value):
    """0.009323823 -> '0,009324' — German decimal comma, as the dashboard shows it."""
    try:
        return f"{float(value):.4g}".replace(".", ",")
    except (TypeError, ValueError):
        return str(value)


def de_date(iso_date):
    try:
        return datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return iso_date


def headline(row):
    parts = [f"KW {row['iso_week']}", row["category"], de_number(row["wval"])]
    if row["trend"]:
        parts.append(f"{row['trend']} {row['percent_change']}".strip())
    return " · ".join(p for p in parts if p)


def slugify(name):
    """ASCII slug — tag: URIs and filenames must not carry umlauts."""
    folded = name.lower()
    for src, dst in (("ü", "ue"), ("ö", "oe"), ("ä", "ae"), ("ß", "ss")):
        folded = folded.replace(src, dst)
    return re.sub(r"[^a-z0-9]+", "-", folded).strip("-")


def entry_id(row, pathogen, location):
    return f"tag:{FEED_AUTHORITY},{row['sample_date']}:{slugify(location)}:{pathogen.lower()}"


def published_at(sample_date):
    try:
        day = datetime.strptime(sample_date, "%Y-%m-%d")
    except ValueError:
        return sample_date
    return day.replace(hour=12, tzinfo=BERLIN).isoformat(timespec="seconds")


def updated_at(row):
    stamp = row.get("fetched_at") or ""
    return stamp if stamp else published_at(row["sample_date"])


def build_feed(rows, site_url, pathogen, location):
    ET.register_namespace("", ATOM)
    feed = ET.Element(f"{{{ATOM}}}feed")

    def sub(parent, tag, text=None, **attrs):
        el = ET.SubElement(parent, f"{{{ATOM}}}{tag}", attrs)
        if text is not None:
            el.text = text
        return el

    newest = list(reversed(rows))[:MAX_FEED_ENTRIES]

    sub(feed, "title", f"{location} · {pathogen_label(pathogen)} im Abwasser")
    sub(feed, "subtitle", f"Wöchentliche Virusaktivität für {location}, Quelle: LGL Bayern")
    sub(feed, "id", f"tag:{FEED_AUTHORITY},2026:{slugify(location)}:{pathogen.lower()}:feed")
    sub(feed, "updated", updated_at(newest[0]) if newest else
        datetime.now(timezone.utc).isoformat(timespec="seconds"))
    sub(feed, "link", href=f"{site_url}/feed.xml", rel="self", type="application/atom+xml")
    sub(feed, "link", href=DASHBOARD, rel="alternate", type="text/html")
    author = sub(feed, "author")
    sub(author, "name", "Bayerisches Landesamt für Gesundheit und Lebensmittelsicherheit")
    sub(feed, "rights", "Daten: LGL Bayern. Feed erzeugt von niowaste.")

    for row in newest:
        entry = sub(feed, "entry")
        sub(entry, "title", headline(row))
        sub(entry, "id", entry_id(row, pathogen, location))
        sub(entry, "updated", updated_at(row))
        sub(entry, "published", published_at(row["sample_date"]))
        sub(entry, "link", href=DASHBOARD, rel="alternate", type="text/html")
        sub(entry, "content", entry_html(row, location, pathogen), type="html")

    ET.indent(feed, space="  ")
    return ET.tostring(feed, encoding="utf-8", xml_declaration=True)


def entry_html(row, location, pathogen):
    trend = f"<li>Trend: {row['trend']} {row['percent_change']}</li>" if row["trend"] else ""
    return (
        f"<p>Neue Messung für <strong>{location}</strong> "
        f"({pathogen_label(pathogen)}), Probe vom {de_date(row['sample_date'])}.</p>"
        "<ul>"
        f"<li>Virusaktivität: <strong>{row['category']}</strong></li>"
        f"<li>Wochenwert: {de_number(row['wval'])}</li>"
        f"<li>Kalenderwoche: {row['iso_week']}</li>"
        f"{trend}"
        "</ul>"
        f'<p><a href="{DASHBOARD}">Zum Dashboard des LGL Bayern</a></p>'
    )


def pathogen_label(pathogen):
    return {"SARSCOV2": "SARS-CoV-2", "INFLUENZA": "Influenza", "RSV": "RSV"}.get(
        pathogen, pathogen
    )


def build_index(rows, site_url, pathogen, location):
    newest = list(reversed(rows))
    latest = newest[0] if newest else None
    label = pathogen_label(pathogen)

    if latest:
        trend_line = (
            f"<p class=trend>{latest['trend']} {latest['percent_change']}</p>"
            if latest["trend"] else ""
        )
        current = (
            f"<p class=eyebrow>Probe vom {de_date(latest['sample_date'])} · KW {latest['iso_week']}</p>"
            f"<p class=category>{latest['category']}</p>"
            f"<p class=value>{de_number(latest['wval'])}</p>"
            f"{trend_line}"
        )
    else:
        current = "<p class=category>Noch keine Daten erfasst.</p>"

    body_rows = "\n".join(
        "<tr>"
        f"<td>{de_date(r['sample_date'])}</td>"
        f"<td>{r['iso_week']}</td>"
        f"<td>{r['category']}</td>"
        f"<td class=num>{de_number(r['wval'])}</td>"
        f"<td>{r['trend']} {r['percent_change']}</td>"
        "</tr>"
        for r in newest
    )

    return f"""<!doctype html>
<html lang=de>
<meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>{label} im Abwasser · {location}</title>
<link rel=alternate type=application/atom+xml href="{site_url}/feed.xml" title="{location} {label}">
<style>
  :root {{ color-scheme: light dark; --fg:#16181d; --bg:#fbfbf9; --muted:#6b7280; --line:#e3e3df; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --fg:#e8e8e6; --bg:#16181d; --muted:#9aa0aa; --line:#2b2f38; }}
  }}
  body {{ margin:0; padding:2.5rem 1.25rem; background:var(--bg); color:var(--fg);
         font:16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
  main {{ max-width:44rem; margin:0 auto; }}
  h1 {{ font-size:1.35rem; margin:0 0 .25rem; }}
  .eyebrow, .source {{ color:var(--muted); font-size:.875rem; margin:.2rem 0; }}
  .category {{ font-size:2rem; font-weight:600; margin:.75rem 0 0; }}
  .value {{ font-size:1rem; color:var(--muted); margin:.15rem 0; font-variant-numeric:tabular-nums; }}
  .trend {{ margin:.15rem 0; font-size:.9rem; }}
  table {{ border-collapse:collapse; width:100%; margin-top:2rem; font-size:.9rem; }}
  th, td {{ text-align:left; padding:.5rem .6rem; border-bottom:1px solid var(--line); }}
  th {{ color:var(--muted); font-weight:500; }}
  .num {{ font-variant-numeric:tabular-nums; }}
  .wrap {{ overflow-x:auto; }}
  a {{ color:inherit; }}
</style>
<main>
  <h1>{label} im Abwasser · {location}</h1>
  <p class=source>Quelle: <a href="{DASHBOARD}">LGL Bayern, Abwassermonitoring</a> ·
     <a href="{site_url}/feed.xml">Atom-Feed</a></p>
  {current}
  <div class=wrap>
  <table>
    <thead><tr><th>Probe</th><th>KW</th><th>Aktivität</th><th>Wochenwert</th><th>Trend</th></tr></thead>
    <tbody>
{body_rows}
    </tbody>
  </table>
  </div>
</main>
"""


# --------------------------------------------------------------------------- output


def emit_github_output(row, location, pathogen):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    title = f"{location} {pathogen_label(pathogen)}: {headline(row)}"
    body = (
        f"Neue Messung für {location} ({pathogen_label(pathogen)}).\n\n"
        f"| | |\n|---|---|\n"
        f"| Probe | {de_date(row['sample_date'])} |\n"
        f"| Kalenderwoche | {row['iso_week']} |\n"
        f"| Virusaktivität | **{row['category']}** |\n"
        f"| Wochenwert | {de_number(row['wval'])} |\n"
        f"| Trend | {row['trend']} {row['percent_change']} |\n\n"
        f"[Dashboard des LGL Bayern]({DASHBOARD})\n"
    )
    delim = f"EOF_{secrets.token_hex(8)}"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("new_data=true\n")
        fh.write(f"sample_date={row['sample_date']}\n")
        fh.write(f"title<<{delim}\n{title}\n{delim}\n")
        fh.write(f"body<<{delim}\n{body}\n{delim}\n")


def site_url_for(explicit):
    if explicit:
        return explicit.rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo and "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}"
    return "."


# --------------------------------------------------------------------------- main


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pathogen", default="SARSCOV2", choices=["SARSCOV2", "INFLUENZA", "RSV"])
    ap.add_argument("--location", default="München")
    ap.add_argument("--endpoint", default=API_BASE, help="override for testing the failure path")
    ap.add_argument("--site-url", default=None, help="base URL of the published site")
    ap.add_argument("--dry-run", action="store_true", help="fetch and report; write nothing")
    ap.add_argument("--force", action="store_true", help="treat the current reading as new")
    args = ap.parse_args(argv)

    csv_path = ROOT / "data" / f"{slugify(args.location)}-{args.pathogen.lower()}.csv"

    try:
        row = current_reading(args.pathogen, args.location, args.endpoint)
    except EndpointError as exc:
        print(
            "ERROR: the LGL endpoint no longer looks the way this script expects.\n"
            f"  {exc}\n"
            "  This API is undocumented and may have changed. Open\n"
            f"  {DASHBOARD} and update check.py accordingly.",
            file=sys.stderr,
        )
        return 1

    history = read_history(csv_path)
    known = {r["sample_date"] for r in history}
    is_new = args.force or row["sample_date"] not in known

    print(f"{args.location} {pathogen_label(args.pathogen)}: {headline(row)}")

    if args.dry_run:
        if is_new:
            print(f"DRY RUN: would record sample {row['sample_date']} and regenerate the feed")
        else:
            print(f"no new data (latest sample {row['sample_date']} already recorded)")
        return 0

    if is_new:
        history = upsert(history, row)
        write_history(csv_path, history)

    # Always regenerate docs/, even with no new data: it is derived from the CSV, so this is a
    # no-op byte-for-byte unless something actually changed, and it lets the first CI run fix
    # up the site URL without waiting a week for a fresh measurement.
    site_url = site_url_for(args.site_url)
    docs = ROOT / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "feed.xml").write_bytes(build_feed(history, site_url, args.pathogen, args.location))
    (docs / "index.html").write_text(
        build_index(history, site_url, args.pathogen, args.location), encoding="utf-8"
    )

    if not is_new:
        print(f"no new data (latest sample {row['sample_date']} already recorded)")
        return 0

    emit_github_output(row, args.location, args.pathogen)
    print(f"recorded sample {row['sample_date']}; feed and page regenerated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
