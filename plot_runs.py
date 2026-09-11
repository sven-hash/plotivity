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
import webbrowser
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import folium
import polyline
from jinja2 import Template
import requests
from dotenv import load_dotenv

API = "https://www.strava.com/api/v3"
class PlotError(Exception):
    """User-facing error (bad credentials, no data, ...)."""


def _default_log(msg: str) -> None:
    print(msg, file=sys.stderr)


LOG = _default_log  # GUI replaces this to show progress


def log(msg: str) -> None:
    LOG(msg)


PROGRESS = lambda fraction, label: None  # noqa: E731  GUI replaces this; fraction None = indeterminate


def progress(fraction: float | None, label: str) -> None:
    PROGRESS(fraction, label)


def set_data_dir(d: Path) -> None:
    """Put caches and Garmin tokens under `d` (the GUI uses ~/Library/Application Support/RunMap)."""
    global CACHE, GARMIN_CACHE, GARMIN_TOKENS
    d.mkdir(parents=True, exist_ok=True)
    CACHE = d / "activities_cache.json"
    GARMIN_CACHE = d / "garmin_cache.json"
    GARMIN_TOKENS = str(d / "garmin_tokens")


# lowercase substrings matched against Strava sport_type / Garmin typeKey
SPORTS = {
    "run": ["run"],
    "ride": ["ride", "cycling", "biking", "bike"],
    "hike": ["hike", "hiking"],
    "walk": ["walk"],
    "swim": ["swim"],
    "ski": ["ski", "snowboard"],
}
FETCH_MIN_AGE = timedelta(hours=3)
LINE_COLOR = "#eb6834"
# "hot" ramp for the Strava-style heat layer: dark red (1 pass) -> orange -> yellow -> white (many passes)
HEAT_GRADIENT = {0.08: "#5c0a1c", 0.3: "#c41e1e", 0.5: "#f04a10", 0.7: "#ff8c00", 0.86: "#ffd23c", 1.0: "#fff9d9"}
DARK_TILES = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}"
ESRI_ATTR = "Tiles &copy; Esri"

# Leaflet layer that draws every track on one canvas with translucent strokes so overlaps
# accumulate, then recolours the accumulated alpha through HEAT_GRADIENT (same trick as
# Leaflet.heat, but with lines instead of blurred points -> crisp routes like Strava's heatmap).
HEATLINES_JS = r"""
L.HeatLines = L.Layer.extend({
  options: { width: 2, intensity: 0.22, gradient: {}, glow: true },
  initialize: function (tracks, options) {
    L.setOptions(this, options);
    this._tracks = tracks.map(function (pts) { return { pts: pts, bounds: L.latLngBounds(pts) }; });
  },
  onAdd: function (map) {
    this._map = map;
    if (!this._canvas) this._initCanvas();
    map.getPanes().overlayPane.appendChild(this._canvas);
    map.on('moveend resize', this._reset, this);
    if (map.options.zoomAnimation && L.Browser.any3d) map.on('zoomanim', this._animateZoom, this);
    this._reset();
  },
  onRemove: function (map) {
    L.DomUtil.remove(this._canvas);
    map.off('moveend resize', this._reset, this);
    map.off('zoomanim', this._animateZoom, this);
  },
  addTo: function (map) { map.addLayer(this); return this; },
  _initCanvas: function () {
    var c = this._canvas = L.DomUtil.create('canvas', 'leaflet-layer');
    c.style[L.DomUtil.testProp(['transformOrigin', 'WebkitTransformOrigin'])] = '50% 50%';
    c.style.pointerEvents = 'none';
    var animated = this._map.options.zoomAnimation && L.Browser.any3d;
    L.DomUtil.addClass(c, 'leaflet-zoom-' + (animated ? 'animated' : 'hide'));
    this._grad = this._buildGradient();
  },
  _buildGradient: function () {
    var c = document.createElement('canvas'), ctx = c.getContext('2d');
    c.width = 1; c.height = 256;
    var g = ctx.createLinearGradient(0, 0, 0, 256);
    for (var k in this.options.gradient) g.addColorStop(+k, this.options.gradient[k]);
    ctx.fillStyle = g; ctx.fillRect(0, 0, 1, 256);
    return ctx.getImageData(0, 0, 1, 256).data;
  },
  _reset: function () {
    // anchor the canvas at the current viewport's top-left (setPosition also clears any zoom-anim scale)
    L.DomUtil.setPosition(this._canvas, this._map.containerPointToLayerPoint([0, 0]));
    var size = this._map.getSize(), dpr = Math.min(window.devicePixelRatio || 1, 2);
    this._dpr = dpr;
    this._canvas.width = size.x * dpr; this._canvas.height = size.y * dpr;
    this._canvas.style.width = size.x + 'px'; this._canvas.style.height = size.y + 'px';
    this._redraw();
  },
  _redraw: function () {
    var map = this._map, c = this._canvas, ctx = c.getContext('2d'), dpr = this._dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, c.width, c.height);
    var view = map.getBounds().pad(0.05), zoom = map.getZoom();
    var w = this.options.width * (zoom >= 14 ? 1.25 : zoom >= 11 ? 1 : 0.75);
    var passes = this.options.glow ? [[w * 3, this.options.intensity * 0.2], [w, this.options.intensity]]
                                   : [[w, this.options.intensity]];
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    for (var p = 0; p < passes.length; p++) {
      ctx.lineWidth = passes[p][0];
      ctx.strokeStyle = 'rgba(0,0,0,' + passes[p][1] + ')';
      for (var t = 0; t < this._tracks.length; t++) {
        var tr = this._tracks[t];
        if (!view.intersects(tr.bounds)) continue;
        ctx.beginPath();
        for (var i = 0; i < tr.pts.length; i++) {
          var pt = map.latLngToContainerPoint(tr.pts[i]);
          if (i === 0) ctx.moveTo(pt.x, pt.y); else ctx.lineTo(pt.x, pt.y);
        }
        ctx.stroke();   // one stroke per track: overlaps BETWEEN runs accumulate, a run's own out-and-back doesn't
      }
    }
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    var img = ctx.getImageData(0, 0, c.width, c.height), d = img.data, g = this._grad;
    for (var i = 0; i < d.length; i += 4) {
      var a = d[i + 3];
      if (!a) continue;
      var j = a * 4;
      d[i] = g[j]; d[i + 1] = g[j + 1]; d[i + 2] = g[j + 2];
      d[i + 3] = a < 24 ? a * 2 : Math.min(255, 100 + a * 1.2);
    }
    ctx.putImageData(img, 0, 0);
  },
  _animateZoom: function (e) {
    var scale = this._map.getZoomScale(e.zoom),
        offset = this._map._latLngBoundsToNewLayerBounds(this._map.getBounds(), e.zoom, e.center).min;
    L.DomUtil.setTransform(this._canvas, offset, scale);
  }
});
L.heatLines = function (tracks, options) { return new L.HeatLines(tracks, options); };
"""


class HeatToggle(folium.MacroElement):
    """Heatmap checkbox as a mode switch: unchecked -> routes + light basemap,
    checked -> heat + dark basemap."""

    _template = Template(
        """
        {% macro script(this, kwargs) %}
        (function () {
            var map = {{ this._parent.get_name() }}, heat = {{ this.heat.get_name() }};
            var dark = {{ this.dark.get_name() }}, light = {{ this.light.get_name() }};
            var routes = [{% for g in this.groups %}{{ g.get_name() }},{% endfor %}];
            function swapBase(from, to) {
                if (map.hasLayer(from)) { map.removeLayer(from); map.addLayer(to); }
            }
            // setTimeout: run after the layer control has finished processing the click,
            // otherwise it removes the layers we just added (it walks every unchecked box).
            map.on('overlayremove', function (e) {
                if (e.layer !== heat) return;
                setTimeout(function () {
                    routes.forEach(function (l) { map.addLayer(l); });
                    swapBase(dark, light);
                }, 0);
            });
            map.on('overlayadd', function (e) {
                if (e.layer !== heat) return;
                setTimeout(function () {
                    routes.forEach(function (l) { map.removeLayer(l); });
                    swapBase(light, dark);
                }, 0);
            });
        })();
        {% endmacro %}
        """
    )

    def __init__(self, heat, groups, dark, light):
        super().__init__()
        self._name = "HeatToggle"
        self.heat = heat
        self.groups = groups
        self.dark = dark
        self.light = light


class HeatLines(folium.map.Layer):
    """folium wrapper so the canvas layer shows up in LayerControl."""

    _template = Template(
        """
        {% macro script(this, kwargs) %}
        var {{ this.get_name() }} = L.heatLines({{ this.data_var }}, {{ this.opts|tojson }});
        {% endmacro %}
        """
    )

    def __init__(self, data_var: str, name: str, **opts):
        super().__init__(name=name, overlay=True, control=True, show=True)
        self._name = "HeatLines"
        self.data_var = data_var
        self.opts = opts
CACHE = Path("activities_cache.json")


def get_access_token() -> str:
    token = os.getenv("STRAVA_ACCESS_TOKEN")
    if token:
        return token

    client_id = os.getenv("STRAVA_CLIENT_ID")
    client_secret = os.getenv("STRAVA_CLIENT_SECRET")
    refresh_token = os.getenv("STRAVA_REFRESH_TOKEN")
    if not all([client_id, client_secret, refresh_token]):
        raise PlotError(
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
            log("Rate limited by Strava, sleeping 60s...")
            time.sleep(60)
            continue
        if r.status_code == 403:
            body = r.text
            if '"Inactive"' in body:
                raise PlotError(
                    "Strava says your API application is Inactive. Since mid-2026 Strava requires "
                    "the app owner to have a paid Strava subscription for API access.\n"
                    "No-subscription alternative: request your data archive at "
                    "https://www.strava.com/athlete/delete_your_account (Request Your Archive), "
                    "then run:  uv run plot_runs.py --export export_XXXX.zip  (or use Garmin Connect: uv run plot_runs.py)"
                )
            if "activity:read_permission" in body:
                raise PlotError("Token lacks activity:read_all scope. Redo the OAuth flow described in README.md.")
            raise PlotError(f"403 Forbidden from Strava: {body}")
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        activities.extend(batch)
        log(f"Fetched page {page} ({len(activities)} activities so far)")
        page += 1
    return activities


def load_activities(token: str, refresh: bool) -> list[dict]:
    if CACHE.exists() and not refresh:
        log(f"Using cached activities from {CACHE} (pass --refresh to refetch)")
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
        log(f"{len(rows)} activities in export")
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
                log(f"Skipping {fname}: {e}")
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
                    "elevation_gain": num("Elevation Gain"),
                    "map": {"summary_polyline": polyline.encode(pts)},
                    "url": f"https://www.strava.com/activities/{row['Activity ID']}" if row.get("Activity ID") else "",
                }
            )
    return activities


# ---------- Garmin Connect mode (unofficial API, email/password login) ----------

GARMIN_CACHE = Path("garmin_cache.json")
GARMIN_TOKENS = "~/.garminconnect"


def _read_garmin_cache() -> tuple[dict[str, dict], datetime | None, set[str]]:
    """Returns (activities by id, time of last Garmin fetch, sports that fetch covered)."""
    if not GARMIN_CACHE.exists():
        return {}, None, set()
    raw = json.loads(GARMIN_CACHE.read_text())
    if "activities" not in raw:  # old flat format {id: entry}
        return raw, None, set()
    ts = raw.get("fetched_at")
    return raw["activities"], datetime.fromisoformat(ts) if ts else None, set(raw.get("sports", []))


def _write_garmin_cache(cache: dict[str, dict], fetched_at: datetime | None, sports: set[str]) -> None:
    GARMIN_CACHE.write_text(
        json.dumps(
            {
                "fetched_at": fetched_at.isoformat(timespec="seconds") if fetched_at else None,
                "sports": sorted(sports),
                "activities": cache,
            }
        )
    )


def load_garmin(sport: str, refresh: bool, force: bool, email: str | None = None,
                password: str | None = None, mfa_prompt=None) -> list[dict]:
    cache, fetched_at, sports = _read_garmin_cache()
    if refresh:
        cache, sports = {}, set()

    # Skip Garmin entirely if we fetched this sport recently (unless --force / --refresh)
    covered = sport in sports or "all" in sports
    if cache and fetched_at and covered and not force and not refresh:
        age = datetime.now() - fetched_at
        if age < FETCH_MIN_AGE:
            mins = int(age.total_seconds() // 60)
            log(f"Garmin checked {mins} min ago, using cached activities (force to check again)")
            result = [e for e in cache.values() if sport_matches(e.get("sport_type", ""), sport)]
            log(f"{len(result)} cached activities match sport={sport}")
            return result

    from garminconnect import Garmin

    email = email or os.getenv("GARMIN_EMAIL")
    password = password or os.getenv("GARMIN_PASSWORD")
    tokens_exist = Path(GARMIN_TOKENS).expanduser().exists()
    if not tokens_exist and (not email or not password):
        raise PlotError("Garmin email and password are required for the first login")

    log("Logging into Garmin Connect..." + (" (using saved session)" if tokens_exist else ""))
    progress(None, "Logging into Garmin Connect…")
    from garminconnect import GarminConnectAuthenticationError

    client = Garmin(email, password, prompt_mfa=mfa_prompt)
    try:
        client.login(GARMIN_TOKENS)
    except GarminConnectAuthenticationError as e:
        raise PlotError(f"Garmin login failed: {e}") from e

    # 1. list all activities (newest first, 100 per page)
    listed: list[dict] = []
    start = 0
    while True:
        batch = client.get_activities(start, 100)
        if not batch:
            break
        listed.extend(batch)
        log(f"Listed {len(listed)} activities...")
        progress(None, f"Listing activities… {len(listed)}")
        start += len(batch)
    fetched_at = datetime.now()
    sports.add(sport)

    # 2. download GPX for the ones we want and don't have yet
    result: list[dict] = []
    new = 0
    todo = sum(
        1 for a in listed
        if sport_matches((a.get("activityType") or {}).get("typeKey", ""), sport)
        and a.get("hasPolyline", True) and str(a["activityId"]) not in cache
    )
    progress(0.0 if todo else None, f"Downloading {todo} new tracks…" if todo else "No new activities")
    for a in listed:
        aid = str(a["activityId"])
        type_key = (a.get("activityType") or {}).get("typeKey", "")
        if not sport_matches(type_key, sport):
            continue
        if not a.get("hasPolyline", True):
            continue
        if aid in cache:
            cache[aid].setdefault("url", f"https://connect.garmin.com/modern/activity/{aid}")
            cache[aid].setdefault("elevation_gain", a.get("elevationGain") or 0)
            result.append(cache[aid])
            continue
        try:
            gpx = client.download_activity(aid, dl_fmt=Garmin.ActivityDownloadFormat.GPX)
            pts = _parse_gpx(gpx)
        except Exception as e:  # noqa: BLE001
            log(f"Skipping {aid} ({a.get('activityName')}): {e}")
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
            "elevation_gain": a.get("elevationGain") or 0,
            "map": {"summary_polyline": polyline.encode(pts[::step])},
            "url": f"https://connect.garmin.com/modern/activity/{aid}",
        }
        cache[aid] = entry
        result.append(entry)
        new += 1
        progress(new / todo, f"Downloading tracks… {new}/{todo}")
        if new % 10 == 0:
            _write_garmin_cache(cache, fetched_at, sports)
            log(f"Downloaded {new} new tracks...")
        time.sleep(0.3)  # be gentle with Garmin

    _write_garmin_cache(cache, fetched_at, sports)
    log(f"{len(listed)} activities on Garmin, {len(result)} with tracks ({new} newly downloaded)")
    return result

def _short_date(d: str) -> str:
    return d[:10] if "T" in d else d.rsplit(",", 1)[0]


def sport_matches(sport_type: str, sport: str) -> bool:
    if sport == "all":
        return True
    t = (sport_type or "").lower()
    return any(k in t for k in SPORTS[sport])


def _parse_date(d: str) -> date | None:
    if not d:
        return None
    if "T" in d:
        return date.fromisoformat(d[:10])
    try:  # Strava export: "Sep 1, 2026, 7:00:00 AM"
        return datetime.strptime(d.rsplit(",", 1)[0], "%b %d, %Y").date()
    except ValueError:
        return None


def _parse_cli_date(v: str) -> date:
    v = v.strip()
    if len(v) == 4:
        return date(int(v), 1, 1)
    if len(v) == 7:
        return date(int(v[:4]), int(v[5:7]), 1)
    return date.fromisoformat(v)


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


def build_map(runs: list[dict], out: Path, line_weight: float, opacity: float, heatmap: bool = True, sport: str = "run") -> None:
    progress(None, "Building map…")
    tracks = []
    for a in runs:
        encoded = (a.get("map") or {}).get("summary_polyline")
        if not encoded:
            continue
        pts = polyline.decode(encoded)
        if pts:
            tracks.append((a, pts))

    if not tracks:
        raise PlotError("No runs with GPS data found.")

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
    dark = folium.TileLayer(DARK_TILES, attr=ESRI_ATTR, name="Dark", max_native_zoom=16, max_zoom=19, show=heatmap)
    dark.add_to(m)
    light = folium.TileLayer("Esri.WorldGrayCanvas", name="Light", max_native_zoom=16, max_zoom=19, show=not heatmap)
    light.add_to(m)
    folium.TileLayer("Esri.WorldStreetMap", name="Streets", max_zoom=19, show=False).add_to(m)
    folium.TileLayer("Esri.WorldImagery", name="Satellite", max_zoom=19, show=False).add_to(m)

    # One layer per year so years can be toggled; a single hue throughout so
    # overlapping routes build up into a density picture.
    by_year: dict[str, list] = {}
    for a, pts in tracks:
        by_year.setdefault(_year(a), []).append((a, pts))

    year_groups: list[folium.FeatureGroup] = []
    for year in sorted(by_year, reverse=True):
        items = by_year[year]
        km = sum(a.get("distance", 0) for a, _ in items) / 1000
        fg = folium.FeatureGroup(name=f"{year} · {len(items)} runs · {km:,.0f} km", show=not heatmap)
        for a, pts in items:
            dist = a.get("distance", 0)
            secs = a.get("moving_time", 0)
            date = _short_date(a.get("start_date_local", ""))
            name = html.escape(a.get("name") or "Run")
            tooltip = f"{name} · {date} · {dist / 1000:.1f} km"
            link = f'<br><a href="{a["url"]}" target="_blank">Open activity ↗</a>' if a.get("url") else ""
            elev = f" · ↑ {a['elevation_gain']:.0f} m" if a.get("elevation_gain") else ""
            popup = folium.Popup(
                f"<b>{name}</b><br>{date}<br>{dist / 1000:.2f} km · {secs / 60:.0f} min · {_fmt_pace(dist, secs)}{elev}{link}",
                max_width=260,
            )
            folium.PolyLine(
                pts, color=LINE_COLOR, weight=line_weight, opacity=opacity, tooltip=tooltip, popup=popup
            ).add_to(fg)
        fg.add_to(m)
        year_groups.append(fg)

    heat = None
    if heatmap:
        data = [[(round(p[0], 5), round(p[1], 5)) for p in pts] for _, pts in tracks]
        # body-script section: rendered after Leaflet is loaded and before the map code
        m.get_root().script.add_child(folium.Element(HEATLINES_JS))
        m.get_root().script.add_child(
            folium.Element(f"var heatTracks = {json.dumps(data, separators=(',', ':'))};")
        )
        heat = HeatLines("heatTracks", "Heatmap · where you go most", width=1.6, intensity=0.1, gradient=HEAT_GRADIENT)
        heat.add_to(m)

    folium.LayerControl(collapsed=False).add_to(m)
    if heat is not None:
        # Heatmap checkbox acts as a mode switch: off -> show all route layers, on -> hide them.
        HeatToggle(heat, year_groups, dark, light).add_to(m)
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
  <h1>All {html.escape(sport if sport != "all" else "activitie")}s {span}</h1>
  <div class="row">
    <div><div class="n">{len(tracks):,}</div><div class="l">{"activities" if sport == "all" else sport + "s"}</div></div>
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
    p.add_argument("--force", action="store_true", help="Check Garmin for new activities even if fetched < 3 h ago")
    p.add_argument("--refresh", action="store_true", help="Throw away the cache and re-download everything")
    p.add_argument("--sport", choices=[*SPORTS, "all"], default="run", help="Activity type to plot (default: run)")
    p.add_argument("--since", type=_parse_cli_date, metavar="DATE", help="Only activities on/after DATE (2024, 2024-06, 2024-06-15)")
    p.add_argument("--until", type=_parse_cli_date, metavar="DATE", help="Only activities on/before DATE")
    p.add_argument("--no-heatmap", action="store_true", help="Skip the heatmap layer (smaller file, light basemap)")
    p.add_argument("--open", action="store_true", help="Open the map in your browser when done")
    p.add_argument("--weight", type=float, default=2.0, help="Route line width")
    p.add_argument("--opacity", type=float, default=0.55, help="Route line opacity")
    args = p.parse_args()
    try:
        run_cli(args)
    except PlotError as e:
        sys.exit(str(e))


def run_cli(args) -> None:
    if args.export:
        activities = load_export(args.export)
    elif args.strava:
        token = get_access_token()
        activities = load_activities(token, args.refresh)
        for a in activities:
            if a.get("id"):
                a["url"] = f"https://www.strava.com/activities/{a['id']}"
            if a.get("total_elevation_gain"):
                a["elevation_gain"] = a["total_elevation_gain"]
    else:
        activities = load_garmin(args.sport, args.refresh, args.force)

    selected = [a for a in activities if sport_matches(a.get("sport_type", a.get("type", "")), args.sport)]
    if args.since or args.until:
        def in_range(a: dict) -> bool:
            d = _parse_date(a.get("start_date_local", ""))
            if d is None:
                return False
            return (not args.since or d >= args.since) and (not args.until or d <= args.until)
        selected = [a for a in selected if in_range(a)]
    log(f"{len(activities)} activities total, {len(selected)} selected")
    build_map(selected, args.output, args.weight, args.opacity, heatmap=not args.no_heatmap, sport=args.sport)
    if args.open:
        webbrowser.open(args.output.resolve().as_uri())


if __name__ == "__main__":
    main()
