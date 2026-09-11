"""Run Map desktop app: a small native window around plot_runs.py (pywebview)."""

from __future__ import annotations

import json
import shutil
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

import webview

import plot_runs

APP_NAME = "Run Map"
DATA_DIR = Path.home() / "Library" / "Application Support" / "RunMap"
SETTINGS_FILE = DATA_DIR / "settings.json"
MAP_FILE = DATA_DIR / "runs_map.html"


def resource(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / name


class Api:
    """Methods callable from the page as window.pywebview.api.<name>()."""

    def __init__(self) -> None:
        self.window: webview.Window | None = None
        self._busy = False
        self._mfa_event = threading.Event()
        self._mfa_code: str | None = None

    # ---- helpers ----
    def _js(self, code: str) -> None:
        if self.window:
            self.window.evaluate_js(code)

    def _log(self, msg: str) -> None:
        self._js(f"ui.log({json.dumps(msg)})")

    def _has_session(self) -> bool:
        return Path(plot_runs.GARMIN_TOKENS).exists()

    # ---- called from JS ----
    def get_state(self) -> dict:
        settings = {}
        if SETTINGS_FILE.exists():
            try:
                settings = json.loads(SETTINGS_FILE.read_text())
            except json.JSONDecodeError:
                settings = {}
        return {
            "settings": settings,
            "has_session": self._has_session(),
            "has_map": MAP_FILE.exists(),
            "data_dir": str(DATA_DIR),
        }

    def build(self, opts: dict) -> dict:
        if self._busy:
            return {"ok": False, "error": "Already running"}
        self._busy = True
        threading.Thread(target=self._build, args=(opts,), daemon=True).start()
        return {"ok": True}

    def submit_mfa(self, code: str) -> None:
        self._mfa_code = code.strip()
        self._mfa_event.set()

    def open_map(self) -> None:
        webview.create_window(f"{APP_NAME} · map", url=str(MAP_FILE), width=1200, height=800)

    def open_in_browser(self) -> None:
        webbrowser.open(MAP_FILE.as_uri())

    def save_map_as(self) -> dict:
        result = self.window.create_file_dialog(webview.SAVE_DIALOG, save_filename="runs_map.html")
        if not result:
            return {"ok": False}
        dest = Path(result if isinstance(result, str) else result[0])
        shutil.copyfile(MAP_FILE, dest)
        return {"ok": True, "path": str(dest)}

    def forget_login(self) -> None:
        shutil.rmtree(plot_runs.GARMIN_TOKENS, ignore_errors=True)
        Path(plot_runs.GARMIN_CACHE).unlink(missing_ok=True)
        if SETTINGS_FILE.exists():
            s = json.loads(SETTINGS_FILE.read_text())
            s.pop("email", None)
            SETTINGS_FILE.write_text(json.dumps(s))

    # ---- worker ----
    def _ask_mfa(self) -> str:
        self._mfa_event.clear()
        self._js("ui.askMfa()")
        self._mfa_event.wait()
        return self._mfa_code or ""

    def _build(self, opts: dict) -> None:
        plot_runs.LOG = self._log
        try:
            settings = {k: opts.get(k) for k in ("email", "sport", "since", "until", "heatmap")}
            SETTINGS_FILE.write_text(json.dumps(settings))  # never the password

            sport = opts.get("sport") or "run"
            activities = plot_runs.load_garmin(
                sport,
                refresh=bool(opts.get("refresh")),
                force=bool(opts.get("force")),
                email=opts.get("email") or None,
                password=opts.get("password") or None,
                mfa_prompt=self._ask_mfa,
            )
            selected = [a for a in activities if plot_runs.sport_matches(a.get("sport_type", ""), sport)]
            since = plot_runs._parse_cli_date(opts["since"]) if opts.get("since") else None
            until = plot_runs._parse_cli_date(opts["until"]) if opts.get("until") else None
            if since or until:
                def in_range(a: dict) -> bool:
                    d = plot_runs._parse_date(a.get("start_date_local", ""))
                    return d is not None and (not since or d >= since) and (not until or d <= until)
                selected = [a for a in selected if in_range(a)]
            self._log(f"{len(selected)} activities selected")
            plot_runs.build_map(selected, MAP_FILE, 2.0, 0.55, heatmap=bool(opts.get("heatmap", True)), sport=sport)
            self._js("ui.done()")
        except plot_runs.PlotError as e:
            self._js(f"ui.fail({json.dumps(str(e))})")
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._js(f"ui.fail({json.dumps(f'{type(e).__name__}: {e}')})")
        finally:
            self._busy = False


def main() -> None:
    plot_runs.set_data_dir(DATA_DIR)
    api = Api()
    api.window = webview.create_window(
        APP_NAME, url=str(resource("ui.html")), js_api=api, width=520, height=680, resizable=True, min_size=(460, 560)
    )
    webview.start()


if __name__ == "__main__":
    main()
