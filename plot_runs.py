"""Overlay every run on a single interactive map.

Sources: Garmin Connect (default), Strava API (--strava), Strava bulk export (--export ZIP).

Garmin auth (in .env):
      GARMIN_EMAIL=...
      GARMIN_PASSWORD=...
  (MFA code is prompted on first run; tokens are cached in ~/.garminconnect)

Strava auth (in .env, only for --strava):
  Option A - short-lived token (expires after 6h):
      STRAVA_ACCESS_TOKEN=...
  Option B - long-lived, auto-refreshing (recommended):
      STRAVA_CLIENT_ID=...
      STRAVA_CLIENT_SECRET=...
      STRAVA_REFRESH_TOKEN=...
"""

from __future__ import annotations

import argparse
import csv
import html
import gzip
import io
import json
import os
import re
import statistics
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import folium
import polyline
import requests
from dotenv import load_dotenv

API = "https://www.strava.com/api/v3"
RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}
LINE_COLOR = "#eb6834"
CACHE = Path("activities_cache.json")


def get_access_token() -> str:
    token = os.getenv("STRAVA_ACCESS_TOKEN")
    if token:
        return token

    client_id = os.getenv("STRAVA_CLIENT_ID")
    client_secret = os.getenv("STRAVA_CLIENT_SECRET")
    refresh_token = os.getenv("STRAVA_REFRESH_TOKEN")
    if not all([client_id, client_secret, refresh_token]):
        sys.exit(
            "Missing credentials. Set STRAVA_ACCESS_TOKEN, or "
            "STRAVA_CLIENT_ID + STRAVA_CLIENT_SECRET + STRAVA_REFRESH_TOKEN in .env"
        )

    r = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def fetch_activities(token: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"}
    activities: list[dict] = []
    page = 1
    while True:
        r = requests.get(
            f"{API}/athlete/activities",
            headers=headers,
            params={"per_page": 200, "page": page},
            timeout=60,
        )
        if r.status_code == 429:
            print("Rate limited by Strava, sleeping 60s...", file=sys.stderr)
            time.sleep(60)
            continue
        if r.status_code == 403:
            body = r.text
            if '"Inactive"' in body:
                sys.exit(
                    "Strava says your API application is Inactive. Since mid-2026 Strava requires "
                    "the app owner to have a paid Strava subscription for API access.\n"
                    "No-subscription alternative: request your data archive at "
                    "https://www.strava.com/athlete/delete_your_account (Request Your Archive), "
                    "then run:  uv run plot_runs.py --export export_XXXX.zip  (or use Garmin Connect: uv run plot_runs.py)"
                )
            if "activity:read_permission" in body:
                sys.exit("Token lacks activity:read_all scope. Redo the OAuth flow described in README.md.")
            sys.exit(f"403 Forbidden from Strava: {body}")
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        activities.extend(batch)
        print(f"Fetched page {page} ({len(activities)} activities so far)", file=sys.stderr)
        page += 1
    return activities


def load_activities(token: str, refresh: bool) -> list[dict]:
    if CACHE.exists() and not refresh:
        print(f"Using cached activities from {CACHE} (pass --refresh to refetch)", file=sys.stderr)
        return json.loads(CACHE.read_text())
    activities = fetch_activities(token)
    CACHE.write_text(json.dumps(activities))
    return activities


# ---------- Offline mode: Strava bulk export (no API / subscription needed) ----------

def _parse_gpx(data: bytes) -> list[tuple[float, float]]:
    root = ET.fromstring(data)
    pts = []
    for el in root.iter():
        if el.tag.endswith("trkpt"):
            pts.append((float(el.get("lat")), float(el.get("lon"))))
    return pts


def _parse_tcx(data: bytes) -> list[tuple[float, float]]:
    root = ET.fromstring(data)
    pts = []
    for el in root.iter():
        if el.tag.endswith("Position"):
            lat = lon = None
            for c in el:
                if c.tag.endswith("LatitudeDegrees"):
                    lat = float(c.text)
                elif c.tag.endswith("LongitudeDegrees"):
                    lon = float(c.text)
            if lat is not None and lon is not None:
                pts.append((lat, lon))
    return pts


def _parse_fit(data: bytes) -> list[tuple[float, float]]:
    import fitdecode

    pts = []
    scale = 180 / 2**31
    with fitdecode.FitReader(io.BytesIO(data)) as fit:
        for frame in fit:
            if isinstance(frame, fitdecode.FitDataMessage) and frame.name == "record":
                lat = frame.get_value("position_lat", fallback=None)
                lon = frame.get_value("position_long", fallback=None)
                if lat is not None and lon is not None:
                    pts.append((lat * scale, lon * scale))
    return pts


def load_export(zip_path: Path) -> list[dict]:
    """Read activities.csv + track files from a Strava data-archive zip.

    Returns activity dicts shaped like the API ones (with map.summary_polyline).
    """
    activities = []
    with zipfile.ZipFile(zip_path) as z:
        with z.open("activities.csv") as f:
            rows = list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8")))
        print(f"{len(rows)} activities in export", file=sys.stderr)
        for row in rows:
            fname = row.get("Filename") or ""
            if not fname:
                continue
            try:
                raw = z.read(fname)
            except KeyError:
                continue
            if fname.endswith(".gz"):
                raw = gzip.decompress(raw)
                fname = fname[:-3]
            try:
                if fname.endswith(".gpx"):
                    pts = _parse_gpx(raw)
                elif fname.endswith(".tcx"):
                    pts = _parse_tcx(raw)
                elif fname.endswith(".fit"):
                    pts = _parse_fit(raw)
                else:
                    continue
            except Exception as e:  # noqa: BLE001
                print(f"Skipping {fname}: {e}", file=sys.stderr)
                continue
            if not pts:
                continue
            # thin dense tracks so the HTML stays small
            step = max(1, len(pts) // 300)
            pts = pts[::step]

            def num(key: str) -> float:
                try:
                    return float(row.get(key) or 0)
                except ValueError:
                    return 0.0

            activities.append(
                {
                    "name": row.get("Activity Name", "Run"),
                    "sport_type": (row.get("Activity Type") or "").replace(" ", ""),
                    "start_date_local": row.get("Activity Date", ""),
                    "distance": num("Distance") * 1000,  # csv Distance is km
                    "moving_time": num("Moving Time"),
                    "map": {"summary_polyline": polyline.encode(pts)},
                    "url": f"https://www.strava.com/activities/{row['Activity ID']}" if row.get("Activity ID") else "",
                }
            )
    return activities


# ---------- Garmin Connect mode (unofficial API, email/password login) ----------

GARMIN_CACHE = Path("garmin_cache.json")
GARMIN_TOKENS = "~/.garminconnect"


def load_garmin(all_sports: bool, refresh: bool) -> list[dict]:
    from garminconnect import Garmin

    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    if not email or not password:
        sys.exit("Set GARMIN_EMAIL and GARMIN_PASSWORD in .env")

    print("Logging into Garmin Connect (tokens cached in ~/.garminconnect)...", file=sys.stderr)
    client = Garmin(email, password)
    client.login(GARMIN_TOKENS)

    cache: dict[str, dict] = {}
    if GARMIN_CACHE.exists() and not refresh:
        cache = json.loads(GARMIN_CACHE.read_text())

    # 1. list all activities (newest first, 100 per page)
    listed: list[dict] = []
    start = 0
    while True:
        batch = client.get_activities(start, 100)
        if not batch:
            break
        listed.extend(batch)
        print(f"Listed {len(listed)} activities...", file=sys.stderr)
        start += len(batch)

    # 2. download GPX for the ones we want and don't have yet
    result: list[dict] = []
    new = 0
    for a in listed:
        aid = str(a["activityId"])
        type_key = (a.get("activityType") or {}).get("typeKey", "")
        if not all_sports and "run" not in type_key:
            continue
        if not a.get("hasPolyline", True):
            continue
        if aid in cache:
            cache[aid].setdefault("url", f"https://connect.garmin.com/modern/activity/{aid}")
            result.append(cache[aid])
            continue
        try:
            gpx = client.download_activity(aid, dl_fmt=Garmin.ActivityDownloadFormat.GPX)
            pts = _parse_gpx(gpx)
        except Exception as e:  # noqa: BLE001
            print(f"Skipping {aid} ({a.get('activityName')}): {e}", file=sys.stderr)
            continue
        if not pts:
            continue
        step = max(1, len(pts) // 300)
        entry = {
            "name": a.get("activityName") or type_key,
            "sport_type": type_key,
            "start_date_local": (a.get("startTimeLocal") or "").replace(" ", "T"),
            "distance": a.get("distance") or 0,
            "moving_time": a.get("movingDuration") or a.get("duration") or 0,
            "map": {"summary_polyline": polyline.encode(pts[::step])},
            "url": f"https://connect.garmin.com/modern/activity/{aid}",
        }
        cache[aid] = entry
        result.append(entry)
        new += 1
        if new % 10 == 0:
            GARMIN_CACHE.write_text(json.dumps(cache))
            print(f"Downloaded {new} new tracks...", file=sys.stderr)
        time.sleep(0.3)  # be gentle with Garmin

    GARMIN_CACHE.write_text(json.dumps(cache))
    print(f"{len(listed)} activities on Garmin, {len(result)} with tracks ({new} newly downloaded)", file=sys.stderr)
    return result


def _short_date(d: str) -> str:
    return d[:10] if "T" in d else d.rsplit(",", 1)[0]


def _year(a: dict) -> str:
    d = a.get("start_date_local", "")
    if "T" in d:
        return d[:4]
    m = re.search(r"(20\d\d|19\d\d)", d)
    return m.group(1) if m else "unknown"


def _fmt_pace(dist_m: float, secs: float) -> str:
    if not dist_m or not secs:
        return ""
    s_per_km = secs / (dist_m / 1000)
    return f"{int(s_per_km // 60)}:{int(s_per_km % 60):02d} /km"


def _core_bounds(tracks: list[tuple[dict, list]]) -> list[tuple[float, float]]:
    """Bounds of the main cluster of runs, so trips abroad don't zoom the map out to a continent."""
    centers = [(sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)) for _, pts in tracks]
    lat_c = statistics.median(c[0] for c in centers)
    lon_c = statistics.median(c[1] for c in centers)
    core = [pts for (_, pts), c in zip(tracks, centers) if abs(c[0] - lat_c) < 1 and abs(c[1] - lon_c) < 1]
    pts = [p for t in core for p in t]
    return [(min(p[0] for p in pts), min(p[1] for p in pts)), (max(p[0] for p in pts), max(p[1] for p in pts))]


def build_map(runs: list[dict], out: Path, line_weight: float, opacity: float) -> None:
    tracks = []
    for a in runs:
        encoded = (a.get("map") or {}).get("summary_polyline")
        if not encoded:
            continue
        pts = polyline.decode(encoded)
        if pts:
            tracks.append((a, pts))

    if not tracks:
        sys.exit("No runs with GPS data found.")

    all_pts = [p for _, pts in tracks for p in pts]
    all_bounds = [
        (min(p[0] for p in all_pts), min(p[1] for p in all_pts)),
        (max(p[0] for p in all_pts), max(p[1] for p in all_pts)),
    ]
    core_bounds = _core_bounds(tracks)

    # prefer_canvas: hundreds of tracks render far faster on canvas than as SVG.
    # Esri tiles: OpenStreetMap's servers reject requests without a Referer, which
    # is what a browser sends when opening a local file:// page (403 "Access blocked").
    m = folium.Map(tiles=None, prefer_canvas=True, control_scale=True, zoom_control="bottomright")
    folium.TileLayer("Esri.WorldGrayCanvas", name="Light", max_native_zoom=16, max_zoom=19).add_to(m)
    folium.TileLayer("Esri.WorldStreetMap", name="Streets", max_zoom=19, show=False).add_to(m)
    folium.TileLayer("Esri.WorldImagery", name="Satellite", max_zoom=19, show=False).add_to(m)

    # One layer per year so years can be toggled; a single hue throughout so
    # overlapping routes build up into a density picture.
    by_year: dict[str, list] = {}
    for a, pts in tracks:
        by_year.setdefault(_year(a), []).append((a, pts))

    for year in sorted(by_year, reverse=True):
        items = by_year[year]
        km = sum(a.get("distance", 0) for a, _ in items) / 1000
        fg = folium.FeatureGroup(name=f"{year} · {len(items)} runs · {km:,.0f} km")
        for a, pts in items:
            dist = a.get("distance", 0)
            secs = a.get("moving_time", 0)
            date = _short_date(a.get("start_date_local", ""))
            name = html.escape(a.get("name") or "Run")
            tooltip = f"{name} · {date} · {dist / 1000:.1f} km"
            link = f'<br><a href="{a["url"]}" target="_blank">Open activity ↗</a>' if a.get("url") else ""
            popup = folium.Popup(
                f"<b>{name}</b><br>{date}<br>{dist / 1000:.2f} km · {secs / 60:.0f} min · {_fmt_pace(dist, secs)}{link}",
                max_width=260,
            )
            folium.PolyLine(
                pts, color=LINE_COLOR, weight=line_weight, opacity=opacity, tooltip=tooltip, popup=popup
            ).add_to(fg)
        fg.add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)
    m.fit_bounds(core_bounds)

    total_km = sum(a.get("distance", 0) for a, _ in tracks) / 1000
    total_h = sum(a.get("moving_time", 0) for a, _ in tracks) / 3600
    years = sorted(by_year)
    span = f"{years[0]}–{years[-1]}" if len(years) > 1 else years[0]
    map_var = m.get_name()
    panel = f"""
<style>
  .run-stats {{
    position: absolute; top: 12px; left: 12px; z-index: 1000;
    background: rgba(255,255,255,0.94); color: #0b0b0b;
    font: 13px/1.4 -apple-system, system-ui, "Segoe UI", sans-serif;
    padding: 12px 14px; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,0.25);
    min-width: 170px;
  }}
  .run-stats h1 {{ font-size: 14px; margin: 0 0 8px; font-weight: 600; }}
  .run-stats .n {{ font-size: 22px; font-weight: 600; line-height: 1.1; }}
  .run-stats .l {{ color: #52514e; font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }}
  .run-stats .row {{ display: flex; gap: 16px; margin-bottom: 8px; }}
  .run-stats button {{
    margin-top: 4px; width: 100%; padding: 5px 8px; border: 1px solid #ccc; border-radius: 6px;
    background: #fff; cursor: pointer; font: inherit; font-size: 12px;
  }}
  .run-stats button:hover {{ background: #f2f2f0; }}
  .leaflet-control-layers {{ font-size: 12px; }}
</style>
<div class="run-stats">
  <h1>All runs {span}</h1>
  <div class="row">
    <div><div class="n">{len(tracks):,}</div><div class="l">runs</div></div>
    <div><div class="n">{total_km:,.0f}</div><div class="l">km</div></div>
    <div><div class="n">{total_h:,.0f}</div><div class="l">hours</div></div>
  </div>
  <button onclick="{map_var}.fitBounds({json.dumps(core_bounds)})">Home area</button>
  <button onclick="{map_var}.fitBounds({json.dumps(all_bounds)})">Everywhere</button>
</div>
"""
    m.get_root().html.add_child(folium.Element(panel))
    m.save(str(out))
    print(f"Plotted {len(tracks)} runs ({total_km:.0f} km, {len(years)} years) -> {out}")

def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser(description="Overlay all your runs (Garmin Connect by default) on a map.")
    p.add_argument("-o", "--output", type=Path, default=Path("runs_map.html"))
    src = p.add_mutually_exclusive_group()
    src.add_argument("--strava", action="store_true", help="Use the Strava API instead of Garmin Connect (default)")
    src.add_argument("--export", type=Path, help="Use a Strava data-archive zip (no API needed)")
    p.add_argument("--refresh", action="store_true", help="Refetch everything instead of using the local cache")
    p.add_argument("--all-sports", action="store_true", help="Include every activity type, not just runs")
    p.add_argument("--weight", type=float, default=2.0, help="Line width")
    p.add_argument("--opacity", type=float, default=0.55, help="Line opacity (lower = heatmap-like)")
    args = p.parse_args()

    if args.export:
        activities = load_export(args.export)
    elif args.strava:
        token = get_access_token()
        activities = load_activities(token, args.refresh)
        for a in activities:
            if a.get("id"):
                a["url"] = f"https://www.strava.com/activities/{a['id']}"
    else:
        activities = load_garmin(args.all_sports, args.refresh)
    if args.all_sports:
        selected = activities
    else:
        selected = [
            a for a in activities
            if a.get("sport_type", a.get("type")) in RUN_TYPES or "run" in str(a.get("sport_type", "")).lower()
        ]
    print(f"{len(activities)} activities total, {len(selected)} selected", file=sys.stderr)
    build_map(selected, args.output, args.weight, args.opacity)


if __name__ == "__main__":
    main()
