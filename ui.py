"""Native window for the traffic monitor, rendered through the system WebView.

The window chrome is drawn by the page itself (frameless), so the interface
and the dashboard share one design system and one set of colour tokens
instead of drifting apart across two toolkits. Python owns the data and the
process control; the page only renders what state() hands it and calls back
through the exposed methods.
"""

import ctypes
import io
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import redirect_stdout, redirect_stderr

import paths

TITLE = "Clash 流量监控"

# Selectable windows: span in seconds, and the bucket the sparkline uses.
# Each span is sized so the sparkline lands on roughly two dozen points.
RANGES = {
    "1h": (3600, 180),
    "24h": (86400, 3600),
    "7d": (604800, 21600),
}
DEFAULT_RANGE = "24h"
TOP_ROWS = 5
LIVE_FLOOR = 1               # a connection that moved nothing this tick is not news


def detect_theme():
    """Follow the Windows app theme; fall back to light when unreadable."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        with key:
            light, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return "light" if light else "dark"
    except OSError:
        return "light"


def _tune_window(title):
    """Round the corners and restore the resize border.

    A frameless window is created without WS_THICKFRAME, which also removes
    the invisible grab area along its edges, so the window cannot be resized
    by dragging. Putting the style back keeps the custom title bar while
    letting Windows handle the edges.
    """
    user32, dwm = ctypes.windll.user32, ctypes.windll.dwmapi
    GWL_STYLE, WS_THICKFRAME = -16, 0x00040000
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_FRAMECHANGED = 1, 2, 4, 0x20

    for _ in range(40):
        time.sleep(0.25)
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            continue
        pref = ctypes.c_int(2)      # DWMWCP_ROUND
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
        style = user32.GetWindowLongW(hwnd, GWL_STYLE)
        if not style & WS_THICKFRAME:
            user32.SetWindowLongW(hwnd, GWL_STYLE, style | WS_THICKFRAME)
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                                SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED)
        return


class Api:
    def __init__(self):
        # Underscore-prefixed: pywebview walks the public attributes of the
        # js_api object when it builds the bridge, and a Window exposes a
        # native handle whose properties recurse without end.
        self._window = None
        self._live_client = None
        self._live_prev = None
        self._live_at = None
        self._maximized = False

    # -- data

    def state(self, rng=DEFAULT_RANGE):
        import app

        span, bucket = RANGES.get(rng, RANGES[DEFAULT_RANGE])
        now = int(time.time())
        since = now - span
        previous = since - span

        pid = app.collector_pid()
        out = {"recording": pid is not None, "pid": pid, "theme": detect_theme(),
               "range": rng, "proxied": 0, "direct": 0, "delta": None,
               "coverage": None, "db": 0, "spark": [], "top": []}

        if not os.path.exists(paths.DB):
            return out
        size = os.path.getsize(paths.DB)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(paths.DB + suffix):
                size += os.path.getsize(paths.DB + suffix)
        out["db"] = size

        try:
            db = sqlite3.connect("file:%s?mode=ro" % paths.DB.replace("\\", "/"), uri=True)
        except sqlite3.Error:
            return out
        try:
            out["proxied"], out["direct"] = db.execute(
                "SELECT COALESCE(SUM(CASE WHEN is_proxied=1 THEN down+up END),0),"
                "       COALESCE(SUM(CASE WHEN is_proxied=0 THEN down+up END),0)"
                " FROM minute_agg WHERE minute_ts >= ?", (since,)).fetchone()

            # The same span immediately before this one is the only honest
            # baseline: it shares the time of day and the day of week.
            prior = db.execute(
                "SELECT COALESCE(SUM(down+up),0) FROM minute_agg"
                " WHERE minute_ts >= ? AND minute_ts < ? AND is_proxied = 1",
                (previous, since)).fetchone()[0]
            if prior:
                out["delta"] = 100.0 * (out["proxied"] - prior) / prior
                out["prior"] = prior

            core, seen = db.execute(
                "SELECT COALESCE(SUM(core_down),0), COALESCE(SUM(seen_down),0)"
                " FROM recon WHERE minute_ts >= ?", (since,)).fetchone()
            if core:
                out["coverage"] = 100.0 * seen / core

            start = since // bucket * bucket
            buckets = dict(db.execute(
                "SELECT minute_ts / ? * ?, SUM(down+up) FROM minute_agg"
                " WHERE minute_ts >= ? AND is_proxied = 1 GROUP BY 1",
                (bucket, bucket, start)))
            out["spark"] = [buckets.get(start + i * bucket, 0)
                            for i in range(span // bucket)]

            out["top"] = [
                {"process": p or "(unknown)", "host": h or "(no host)", "bytes": t,
                 "direct": False}
                for p, h, t in db.execute(
                    "SELECT process, host, SUM(down+up) AS t FROM minute_agg"
                    " WHERE minute_ts >= ? AND is_proxied = 1"
                    " GROUP BY process, host ORDER BY t DESC LIMIT ?",
                    (since, TOP_ROWS))]
        except sqlite3.Error:
            pass
        finally:
            db.close()
        return out

    # -- actions

    def toggle(self):
        import app

        if app.collector_pid() is None:
            app.start_collector()
            for _ in range(20):
                time.sleep(0.25)
                if app.collector_pid():
                    return {"message": "已开始记录。"}
            return {"message": "启动失败，请检查 Clash 是否运行。"}
        clean = app.stop_collector()
        return {"message": "记录已停止，数据已写入。" if clean
                           else "采集器未按时退出，已强制结束。"}

    def open_dashboard(self):
        import dashboard

        buf = io.StringIO()
        try:
            with redirect_stdout(buf), redirect_stderr(buf):
                dashboard.main(["--open"])
        except Exception as exc:
            return {"message": "生成失败：%r" % (exc,)}
        return {"message": paths.DASHBOARD}

    def report(self):
        import report as reportmod

        out = os.path.join(paths.BASE, time.strftime("report-%Y%m%d-%H%M.txt"))
        buf = io.StringIO()
        try:
            with redirect_stdout(buf), redirect_stderr(buf):
                reportmod.main(["--since", "24h", "--scope", "all", "--limit", "12"])
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(buf.getvalue())
            os.startfile(out)
        except Exception as exc:
            return {"message": "导出失败：%r" % (exc,)}
        return {"message": out}

    # -- live sampling

    def live_start(self):
        from mihomo_api import PipeClient
        import live as livemod

        self._live_client = PipeClient()
        try:
            self._live_prev, _, down = livemod.snapshot(self._live_client)
        except Exception:
            self._live_prev, down = {}, 0
        self._live_prev_down = down
        self._live_at = time.time()
        return {"ok": True}

    def live_stop(self):
        self._live_client = None
        self._live_prev = None
        return {"ok": True}

    def live_sample(self):
        import live as livemod
        from collector import LOCAL_OUTBOUNDS

        if self._live_client is None:
            return {"total": 0, "rows": []}
        try:
            after, _, down = livemod.snapshot(self._live_client)
        except Exception:
            return {"total": 0, "rows": []}
        now = time.time()
        elapsed = max(now - (self._live_at or now), 0.001)
        total = max(0.0, (down - self._live_prev_down) / elapsed)
        rows = livemod.rates(self._live_prev or {}, after, elapsed)
        self._live_prev, self._live_prev_down, self._live_at = after, down, now
        return {
            "total": total,
            "rows": [{"process": info["process"], "host": info["host"],
                      "bytes": d_down,
                      "direct": info["outbound"] in LOCAL_OUTBOUNDS}
                     for d_down, _up, info in rows[:TOP_ROWS]
                     if d_down >= LIVE_FLOOR],
        }

    # -- window chrome

    def win(self, action):
        if self._window is None:
            return {"ok": False}
        if action == "min":
            self._window.minimize()
        elif action == "max":
            self._window.restore() if self._maximized else self._window.maximize()
            self._maximized = not self._maximized
        elif action == "close":
            self._window.destroy()
        return {"ok": True}


def launch():
    import webview

    api = Api()
    dark = detect_theme() == "dark"
    window = webview.create_window(
        TITLE, paths.resource("ui.html"), js_api=api,
        width=430, height=760, min_size=(380, 560),
        frameless=True, easy_drag=False, resizable=True,
        background_color="#0d0d0d" if dark else "#f4f3f0",
    )
    api._window = window
    threading.Thread(target=_tune_window, args=(TITLE,), daemon=True).start()
    webview.start()
    return 0
