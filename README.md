# strava-plot

Overlay every run you've recorded on Strava onto one interactive map.

> **Note (2026):** Strava now requires the API app owner to have a paid Strava subscription,
> otherwise every call returns `403 ... "Status": "Inactive"`. Without a subscription use the
> bulk-export mode below instead, which needs no API access at all.

## What you get

`runs_map.html` is a standalone interactive map:

- every run drawn in one translucent colour, so places you run often show up darker
- a stats panel (runs, km, hours) with **Home area** / **Everywhere** zoom buttons
- one toggleable layer per year, each labelled with its run count and distance
- hover a run for name, date and distance; click it for pace, time and a link to the activity
- light / streets / satellite basemaps

## Setup

```sh
cp .env.example .env   # then fill it in
uv run plot_runs.py    # writes runs_map.html
```

## Getting credentials

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

   Put the returned `refresh_token` in `.env` along with the client id/secret.
   The script refreshes the access token automatically on every run.

## Options

```
uv run plot_runs.py --refresh          # refetch from Strava (otherwise uses activities_cache.json)
uv run plot_runs.py --all-sports       # include rides, hikes, etc.
uv run plot_runs.py --opacity 0.3      # more heatmap-like
uv run plot_runs.py -o mymap.html
```

## Without the API: bulk export mode

1. Go to https://www.strava.com/athlete/delete_your_account and click **Request Your Archive**
   (this does not delete anything; it emails you a zip within a few hours).
2. Run:

   ```sh
   uv run plot_runs.py --export ~/Downloads/export_12345678.zip
   ```

   Supports GPX, TCX and FIT files (gzipped or not) from the archive.

## Garmin Connect mode

Garmin has no public API for individuals, so this uses the unofficial
[garminconnect](https://github.com/cyberjunky/python-garminconnect) library with your
Garmin Connect email and password.

```sh
# in .env
GARMIN_EMAIL=you@example.com
GARMIN_PASSWORD=...

uv run plot_runs.py --garmin
```

- First run prompts for your MFA code if enabled. Login tokens are cached in `~/.garminconnect`
  so later runs don't need the password or MFA again.
- Every activity's GPX is downloaded once and cached in `garmin_cache.json`; re-runs only
  fetch new activities. `--refresh` re-downloads everything.
- `--all-sports` includes cycling, hiking, etc.
