"""Clash traffic monitor -- windowed front end and command dispatcher.

Double-clicked, this opens the control window. Given a subcommand it behaves
as the command line tool instead, so a single executable covers both:

    ClashTraffic.exe                    the window
    ClashTraffic.exe collect            the recorder the window spawns
    ClashTraffic.exe live --watch
    ClashTraffic.exe report --since 7d
    ClashTraffic.exe dashboard --open
    ClashTraffic.exe status
"""

import ctypes
import io
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
import tkinter as tk
from contextlib import redirect_stdout, redirect_stderr
from tkinter import font as tkfont

import paths

PID_FILE = os.path.join(paths.BASE, "collector.pid")

# A commit refreshes the heartbeat every few seconds; this leaves room for a
# slow poll without reporting a live recorder as dead.
HEARTBEAT_GRACE = 90

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
STILL_ACTIVE = 259

INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
SURFACE = "#fcfcfb"
PANEL = "#f1f0ec"
ACCENT = "#2a78d6"
GOOD = "#0ca30c"
STOPPED = "#898781"
DANGER = "#d03b3b"

UI_FAMILY = "Microsoft YaHei UI"


# ---------------------------------------------------------------- process


def _alive(pid):
    kernel = ctypes.windll.kernel32
    handle = kernel.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel.CloseHandle(handle)
    return bool(ok) and code.value == STILL_ACTIVE


def collector_pid():
    """The recorder's pid, or None when nothing is recording."""
    try:
        with open(PID_FILE) as fh:
            pid = int(fh.read().strip())
        stamp = os.path.getmtime(PID_FILE)
    except (OSError, ValueError):
        return None
    if time.time() - stamp > HEARTBEAT_GRACE:
        return None
    return pid if _alive(pid) else None


def _self_command(*args):
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, os.path.abspath(__file__), *args]


def start_collector():
    subprocess.Popen(
        _self_command("collect", "--quiet", "--commit-every", "10"),
        cwd=paths.BASE, close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
    )


def stop_collector(wait=15.0):
    """Ask the recorder to flush and exit; terminate it only if it will not."""
    pid = collector_pid()
    if pid is None:
        return True
    open(paths.STOP_FILE, "w").close()
    deadline = time.time() + wait
    stopped = False
    while time.time() < deadline:
        time.sleep(0.3)
        if collector_pid() is None:
            stopped = True
            break
    if not stopped:
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x0001, False, pid)   # PROCESS_TERMINATE
        if handle:
            kernel.TerminateProcess(handle, 1)
            kernel.CloseHandle(handle)
    for leftover in (paths.STOP_FILE, PID_FILE):
        try:
            os.remove(leftover)
        except OSError:
            pass
    return stopped


# ---------------------------------------------------------------- summary


def human(n):
    value = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return "%.2f %s" % (value, unit)
        value /= 1024


def summary():
    if not os.path.exists(paths.DB):
        return None
    size = os.path.getsize(paths.DB)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(paths.DB + suffix):
            size += os.path.getsize(paths.DB + suffix)
    try:
        db = sqlite3.connect("file:%s?mode=ro" % paths.DB.replace("\\", "/"), uri=True)
    except sqlite3.Error:
        return None
    try:
        first, last = db.execute(
            "SELECT MIN(minute_ts), MAX(minute_ts) FROM minute_agg").fetchone()
        day = int(time.time()) - 86400
        proxied, direct = db.execute(
            "SELECT COALESCE(SUM(CASE WHEN is_proxied=1 THEN down+up END),0),"
            "       COALESCE(SUM(CASE WHEN is_proxied=0 THEN down+up END),0)"
            " FROM minute_agg WHERE minute_ts >= ?", (day,)).fetchone()
        top = db.execute(
            "SELECT process, SUM(down+up) AS t FROM minute_agg"
            " WHERE minute_ts >= ? AND is_proxied = 1"
            " GROUP BY process ORDER BY t DESC LIMIT 1", (day,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    return {"size": size, "first": first, "last": last,
            "proxied": proxied, "direct": direct, "top": top}


# ---------------------------------------------------------------- window


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.watching = threading.Event()
        self.busy = False

        root.title("Clash 流量监控")
        root.configure(bg=SURFACE)
        root.minsize(620, 560)
        root.geometry("700x660")

        ui = tkfont.Font(family=UI_FAMILY, size=10)
        big = tkfont.Font(family=UI_FAMILY, size=13, weight="bold")
        mono = tkfont.Font(family="Consolas", size=9)

        pad = tk.Frame(root, bg=SURFACE)
        pad.pack(fill="both", expand=True, padx=20, pady=18)

        head = tk.Frame(pad, bg=SURFACE)
        head.pack(fill="x")
        self.dot = tk.Canvas(head, width=14, height=16, bg=SURFACE, highlightthickness=0)
        self.dot.pack(side="left", pady=(4, 0))
        self.dot_id = self.dot.create_oval(2, 4, 12, 14, fill=STOPPED, outline="")
        self.state = tk.Label(head, text="", font=big, bg=SURFACE, fg=INK, anchor="w")
        self.state.pack(side="left", padx=(8, 0))

        self.line1 = tk.Label(pad, text="", font=ui, bg=SURFACE, fg=INK_2, anchor="w")
        self.line1.pack(fill="x", pady=(10, 0))
        self.line2 = tk.Label(pad, text="", font=ui, bg=SURFACE, fg=INK_2, anchor="w")
        self.line2.pack(fill="x", pady=(2, 0))
        self.line3 = tk.Label(pad, text="", font=ui, bg=SURFACE, fg=MUTED, anchor="w")
        self.line3.pack(fill="x", pady=(2, 0))

        self.toggle = tk.Button(pad, text="", font=big, bd=0, cursor="hand2",
                                fg="#ffffff", activeforeground="#ffffff",
                                command=self.on_toggle)
        self.toggle.pack(fill="x", ipady=12, pady=(18, 14))

        row = tk.Frame(pad, bg=SURFACE)
        row.pack(fill="x")
        self.b_dash = self._action(row, ui, "看图表", self.on_dashboard)
        self.b_live = self._action(row, ui, "实时监控", self.on_live)
        self.b_report = self._action(row, ui, "报告", self.on_report)

        box = tk.Frame(pad, bg=PANEL, highlightthickness=1, highlightbackground="#e1e0d9")
        box.pack(fill="both", expand=True, pady=(16, 0))
        bar = tk.Scrollbar(box)
        bar.pack(side="right", fill="y")
        self.out = tk.Text(box, font=mono, bg=PANEL, fg=INK, bd=0, wrap="none",
                           padx=10, pady=8, yscrollcommand=bar.set,
                           state="disabled", height=12)
        self.out.pack(side="left", fill="both", expand=True)
        bar.config(command=self.out.yview)

        self.say("数据目录：%s" % paths.BASE)
        self.refresh()
        self.drain()

    def _action(self, parent, ui, text, command):
        b = tk.Button(parent, text=text, font=ui, bd=0, bg=PANEL, fg=INK_2,
                      cursor="hand2", activebackground="#e4e3de", command=command)
        b.pack(side="left", expand=True, fill="x", ipady=7, padx=(0, 8))
        return b

    # -- output panel

    def say(self, text, clear=False):
        self.out.configure(state="normal")
        if clear:
            self.out.delete("1.0", "end")
        self.out.insert("end", str(text).rstrip("\n") + "\n")
        self.out.see("end")
        self.out.configure(state="disabled")

    def drain(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "text":
                    self.say(payload)
                elif kind == "clear":
                    self.say(payload, clear=True)
                elif kind == "done":
                    self.busy = False
                    self.refresh()
        except queue.Empty:
            pass
        self.root.after(120, self.drain)

    def run_bg(self, fn):
        if self.busy:
            self.say("上一个操作还没结束。")
            return
        self.busy = True
        self.refresh()

        def wrapper():
            try:
                fn()
            except Exception as exc:
                self.events.put(("text", "失败：%r" % (exc,)))
            finally:
                self.events.put(("done", None))

        threading.Thread(target=wrapper, daemon=True).start()

    # -- state

    def refresh(self):
        pid = collector_pid()
        running = pid is not None
        self.dot.itemconfigure(self.dot_id, fill=GOOD if running else STOPPED)
        self.state.configure(
            text=("记录中   pid %d" % pid) if running else "未记录")
        self.toggle.configure(
            text="停 止 记 录" if running else "开 始 记 录",
            bg=DANGER if running else ACCENT,
            activebackground="#b93333" if running else "#1f5fab",
            state="disabled" if self.busy else "normal")
        for b in (self.b_dash, self.b_report):
            b.configure(state="disabled" if self.busy else "normal")
        self.b_live.configure(
            text="停止实时" if self.watching.is_set() else "实时监控")

        s = summary()
        if not s:
            self.line1.configure(text="尚无数据库，开始记录后生成。")
            self.line2.configure(text="")
            self.line3.configure(text="")
            return
        span = "—"
        if s["first"]:
            span = "%s → %s" % (
                time.strftime("%m-%d %H:%M", time.localtime(s["first"])),
                time.strftime("%m-%d %H:%M", time.localtime(s["last"])))
        self.line1.configure(text="数据库 %s · 覆盖 %s"
                                  % (human(s["size"]), span))
        self.line2.configure(text="近 24h 套餐 %s · 直连 %s"
                                  % (human(s["proxied"]), human(s["direct"])))
        self.line3.configure(
            text=("近 24h 最大消耗：%s %s" % (s["top"][0], human(s["top"][1])))
            if s["top"] else "近 24h 没有走代理的流量")

    # -- actions

    def on_toggle(self):
        if collector_pid() is None:
            start_collector()
            self.say("已启动记录。")
            self.root.after(1500, self.refresh)
            return
        self.say("正在请求采集器落盘并退出……")

        def job():
            clean = stop_collector()
            self.events.put(("text", "记录已停止，数据已写入。"
                                     if clean else
                                     "采集器未在限时内退出，已强制结束。"))

        self.run_bg(job)

    def on_dashboard(self):
        import dashboard

        def job():
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(buf):
                dashboard.main(["--open"])
            self.events.put(("clear", buf.getvalue().strip() or "图表已生成。"))
            self.events.put(("text", paths.DASHBOARD))

        self.say("正在生成图表……")
        self.run_bg(job)

    def on_report(self):
        import report

        def job():
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(buf):
                report.main(["--since", "24h", "--scope", "all", "--limit", "10"])
            self.events.put(("clear", buf.getvalue()))

        self.say("正在汇总最近 24 小时……")
        self.run_bg(job)

    def on_live(self):
        if self.watching.is_set():
            self.watching.clear()
            self.refresh()
            return

        import live as livemod
        from mihomo_api import PipeClient

        def job():
            client = PipeClient()
            before, _, down0 = livemod.snapshot(client)
            last = time.time()
            self.events.put(("text", "开始实时采样，再次点击可停止。"))
            while self.watching.is_set():
                time.sleep(2.0)
                try:
                    after, _, down1 = livemod.snapshot(client)
                except Exception as exc:
                    self.events.put(("text", "读取失败：%s" % exc))
                    time.sleep(2.0)
                    continue
                now = time.time()
                elapsed = max(now - last, 0.001)
                rate = (down1 - down0) / elapsed
                rows = livemod.rates(before, after, elapsed)
                before, down0, last = after, down1, now
                if rate < 64 * 1024:
                    continue
                lines = ["%s  合计 %s/s" % (time.strftime("%H:%M:%S"), human(rate))]
                for d_down, _d_up, info in rows[:6]:
                    mark = "" if info["outbound"] in livemod.LOCAL_OUTBOUNDS else "  *套餐"
                    lines.append("   %10s/s  %-18s %-32s %s%s"
                                 % (human(d_down), info["process"][:18],
                                    info["host"][:32], info["outbound"][:12], mark))
                self.events.put(("text", "\n".join(lines)))
            self.events.put(("text", "实时监控已停止。"))

        self.watching.set()
        self.refresh()
        threading.Thread(target=job, daemon=True).start()


def run_gui():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    app = App(root)

    def tick():
        if not app.busy:
            app.refresh()
        root.after(2500, tick)

    root.after(2500, tick)
    root.mainloop()
    return 0


# ---------------------------------------------------------------- dispatch


def _attach_console():
    """A windowed build has no console; borrow the caller's when there is one."""
    try:
        if ctypes.windll.kernel32.AttachConsole(-1):
            sys.stdout = open("CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace")
            sys.stderr = open("CONOUT$", "w", buffering=1, encoding="utf-8", errors="replace")
    except Exception:
        pass


COMMANDS = ("collect", "live", "report", "dashboard", "status")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        try:
            import ui
            return ui.launch()
        except Exception as exc:
            # The WebView runtime can be absent or blocked by policy; the
            # plain window still reaches every action.
            print("webview unavailable (%r), using the plain window" % (exc,),
                  file=sys.stderr)
            return run_gui()

    name, rest = argv[0], argv[1:]
    if name in ("-h", "--help"):
        _attach_console()
        print(__doc__)
        return 0
    if name not in COMMANDS:
        _attach_console()
        print("unknown command %r -- expected one of: %s"
              % (name, ", ".join(COMMANDS)), file=sys.stderr)
        return 2
    if name != "collect":
        _attach_console()

    if name == "collect":
        import collector
        return collector.main(rest)
    if name == "live":
        import live
        return live.main(rest)
    if name == "report":
        import report
        return report.main(rest)
    if name == "dashboard":
        import dashboard
        return dashboard.main(rest)
    import status
    return status.main()


if __name__ == "__main__":
    sys.exit(main())
