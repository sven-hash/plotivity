# strava-plot

Overlay every run you've recorded onto one interactive map. Pulls from **Garmin Connect**
by default; Strava (API or bulk export) is also supported.

## What you get

`runs_map.html` is a standalone interactive map:

- every run drawn in one translucent colour, so places you run often show up darker
- a stats panel (runs, km, hours) with **Home area** / **Everywhere** zoom buttons
- one toggleable layer per year, each labelled with its run count and distance
- hover a run for name, date and distance; click it for pace, time and a link to the activity
- light / streets / satellite basemaps

## Quick start (Garmin Connect)

Garmin has no public API for individuals, so this uses the unofficial
[garminconnect](https://github.com/cyberjunky/python-garminconnect) library with your
Garmin Connect email and password.

```sh
cp .env.example .env    # fill in GARMIN_EMAIL and GARMIN_PASSWORD
uv run plot_runs.py     # writes runs_map.html
open runs_map.html
```

- First run prompts for your MFA code if enabled. Login tokens are cached in `~/.garminconnect`
  so later runs don't need the password or MFA again.
- Every activity's GPX is downloaded once and cached in `garmin_cache.json`; re-runs only
  fetch new activities.

## Options

```
uv run plot_runs.py --refresh          # re-download everything instead of using the cache
uv run plot_runs.py --all-sports       # include rides, hikes, etc.
uv run plot_runs.py --opacity 0.3      # more heatmap-like
uv run plot_runs.py --weight 3         # thicker lines
uv run plot_runs.py -o mymap.html
```

## Strava instead of Garmin

### Bulk export (no API, no subscription)

1. Go to https://www.strava.com/athlete/delete_your_account and click **Request Your Archive**
   (this does not delete anything; it emails you a zip within a few hours).
2. Run:

   ```sh
   uv run plot_runs.py --export ~/Downloads/export_12345678.zip
   ```

   Supports GPX, TCX and FIT files (gzipped or not) from the archive.

### Strava API

> **Note (2026):** Strava now requires the API app owner to have a paid Strava subscription,
> otherwise every call returns `403 ... "Status": "Inactive"`. Without a subscription use the
> bulk-export mode above instead.

1. Create an API app at https://www.strava.com/settings/api (any callback domain, e.g. `localhost`).
   Note the **Client ID** and **Client Secret**.
2. The token shown on that page only has `read` scope, which can't list activities.
   Do a one-time OAuth flow with `activity:read_all`:

   Open this in your browser (replace `CLIENT_ID`):

   ```
   https://www.strava.com/oauth/authorize?client_id=CLIENT_ID&response_type=code&redirect_uri=http://localhost&approval_prompt=force&scope=activity:read_all
   ```

   Approve, then copy the `code=...` from the URL you're redirected to and exchange it:

   ```sh
   curl -X POST https://www.strava.com/oauth/token \
     -d client_id=CLIENT_ID -d client_secret=CLIENT_SECRET \
     -d code=THE_CODE -d grant_type=authorization_code
   ```

   Put the returned `refresh_token` in `.env` along with the client id/secret, then:

   ```sh
   uv run plot_runs.py --strava
   ```

   The script refreshes the access token automatically on every run and caches activities in
   `activities_cache.json`.
