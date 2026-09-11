# strava-plot / Run Map

Overlay every run you've recorded onto one interactive map. Pulls from **Garmin Connect**
by default; Strava (API or bulk export) is also supported.

Two ways to use it: the **Run Map desktop app** (no terminal needed) or the command-line script.

## Run Map app (macOS)

1. Download the DMG from the [Releases](../../releases) page:
   `RunMap-…-AppleSilicon.dmg` for M-series Macs, `RunMap-…-Intel.dmg` for Intel Macs.
   (Apple menu → About This Mac shows which you have.)
2. Open the DMG and drag **Run Map** into **Applications**.
3. **First launch:** the app is not signed with an Apple developer certificate, so macOS blocks it
   once. Open it, click **Done** on the warning, then go to **System Settings → Privacy & Security**,
   scroll down and click **Open Anyway** next to "Run Map". After that it opens normally.
4. Enter your Garmin Connect email and password and click **Build map**. If Garmin asks for a
   verification code, type it in the box that appears. The password is not stored; a login session
   is, so next time you only click Build.
5. **Open map** shows the map in a window; **Save map as…** exports the HTML to share or open in a
   browser.

Data lives in `~/Library/Application Support/RunMap` (login session, cached activities, map).
"Forget saved login and data" in the app removes it.

### Building the app yourself

```sh
./packaging/build_mac.sh            # both architectures (Intel via Rosetta) -> dist/*.dmg
./packaging/build_mac.sh arm64      # just one
```

GitHub Actions (`.github/workflows/build-mac.yml`) builds both DMGs on every push and attaches
them to a GitHub Release when you push a tag like `v0.2.0`:

```sh
git tag v0.2.0 && git push origin v0.2.0
```

## Command-line script

## What you get

`runs_map.html` is a standalone interactive map:

- **Heatmap** (default): dark basemap, routes glow from dark red (run once) through orange to
  yellow-white (run many times), crisp at every zoom. Untick "Heatmap" in the layer control to
  switch to normal mode: every run drawn individually, one toggleable layer per year with its run
  count and distance. Tick it again to go back.
- **Stats panel** (runs, km, hours) with **Home area** / **Everywhere** zoom buttons.
- **Hover** a run for name, date and distance; **click** it for pace, time, elevation gain and a
  link to the activity on Garmin / Strava.
- **Basemaps**: dark, light, streets, satellite.

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
uv run plot_runs.py                    # runs, heatmap, from Garmin (cached if fetched < 3 h ago)
uv run plot_runs.py --force            # check Garmin for new activities even if fetched recently
uv run plot_runs.py --refresh          # throw away the cache and re-download everything
uv run plot_runs.py --sport ride       # run (default) | ride | hike | walk | swim | ski | all
uv run plot_runs.py --since 2025       # only activities from 2025 on (also 2025-06 or 2025-06-15)
uv run plot_runs.py --until 2023-12-31 # only activities up to that date
uv run plot_runs.py --no-heatmap       # routes only, light basemap, smaller file
uv run plot_runs.py --open             # open the map in your browser when done
uv run plot_runs.py --weight 3 --opacity 0.3   # route line style in normal mode
uv run plot_runs.py -o mymap.html
```

**Fetch throttling:** the script remembers when it last talked to Garmin (in `garmin_cache.json`).
If that was less than 3 hours ago it skips the login and listing entirely and builds the map from
the cache, which takes a second. `--force` overrides this. Each sport is tracked separately, so
`--sport ride` after a run-only fetch will still go to Garmin.

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
