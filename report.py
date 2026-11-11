"""Summarise what the collector recorded.

By default it reports traffic that left through a proxy node, which is the
part that spends subscription quota. Pass --scope direct to look at the rest,
or --scope all to compare the two.

Usage:
    python report.py                        last 24h, proxied traffic
    python report.py --since 7d
    python report.py --scope all
    python report.py --host claude.ai       drill into one host
    python report.py --process chrome.exe   drill into one process
    python report.py --json                 machine-readable, for charting
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time

import paths

DEFAULT_DB = paths.DB

_WINDOW = re.compile(r"^(\d+(?:\.\d+)?)([smhdw])$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_window(text):
    match = _WINDOW.match(text.strip().lower())
    if not match:
        raise argparse.ArgumentTypeError(
            "expected a window like 30m, 24h, 7d, got %r" % text)
    return float(match.group(1)) * _UNITS[match.group(2)]


def human(n):
    value = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return "%.2f %s" % (value, unit)
        value /= 1024


def scope_clause(scope):
    if scope == "proxied":
        return "AND is_proxied = 1"
    if scope == "direct":
        return "AND is_proxied = 0"
    return ""


def top_by(db, column, since, scope, limit, extra_where="", params=()):
    sql = (
        "SELECT %s AS k, SUM(down) AS d, SUM(up) AS u"
        " FROM minute_agg WHERE minute_ts >= ? %s %s"
        " GROUP BY k ORDER BY d + u DESC LIMIT ?"
        % (column, scope_clause(scope), extra_where)
    )
    return db.execute(sql, (since,) + tuple(params) + (limit,)).fetchall()


def totals(db, since, scope, extra_where="", params=()):
    sql = (
        "SELECT COALESCE(SUM(down),0), COALESCE(SUM(up),0)"
        " FROM minute_agg WHERE minute_ts >= ? %s %s"
        % (scope_clause(scope), extra_where)
    )
    return db.execute(sql, (since,) + tuple(params)).fetchone()


def print_table(title, rows, width=52):
    print("\n%s" % title)
    print("-" * (width + 26))
    if not rows:
        print("  (nothing recorded)")
        return
    grand = sum(r[1] + r[2] for r in rows) or 1
    for key, down, up in rows:
        label = (key or "(empty)")
        if len(label) > width:
            label = label[: width - 1] + "…"
        share = 100.0 * (down + up) / grand
        print("  %-*s %12s  up %10s  %5.1f%%" % (width, label, human(down), human(up), share))


def coverage(db, since):
    row = db.execute(
        "SELECT COALESCE(SUM(core_down),0), COALESCE(SUM(seen_down),0),"
        "       COALESCE(SUM(core_up),0),   COALESCE(SUM(seen_up),0)"
        " FROM recon WHERE minute_ts >= ?",
        (since,),
    ).fetchone()
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--since", type=parse_window, default=86400.0,
                        help="window ending now, e.g. 30m / 24h / 7d")
    parser.add_argument("--scope", choices=("proxied", "direct", "all"), default="proxied")
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--host", help="restrict to one host")
    parser.add_argument("--process", help="restrict to one process")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of tables")
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print("no database at %s -- run collector.py first" % args.db, file=sys.stderr)
        return 1

    db = sqlite3.connect("file:%s?mode=ro" % args.db.replace("\\", "/"), uri=True)
    since = int((time.time() - args.since) // 60 * 60)

    where, params = "", []
    if args.host:
        where += " AND host = ?"
        params.append(args.host)
    if args.process:
        where += " AND process = ?"
        params.append(args.process)

    down, up = totals(db, since, args.scope, where, params)
    p_down, p_up = totals(db, since, "proxied", where, params)
    d_down, d_up = totals(db, since, "direct", where, params)

    sections = {
        "process": top_by(db, "process", since, args.scope, args.limit, where, params),
        "host": top_by(db, "host", since, args.scope, args.limit, where, params),
        "rule": top_by(db, "rule", since, args.scope, args.limit, where, params),
        "outbound": top_by(db, "outbound", since, args.scope, args.limit, where, params),
    }
    core_down, seen_down, core_up, seen_up = coverage(db, since)

    if args.json:
        print(json.dumps({
            "since": since,
            "scope": args.scope,
            "totals": {"down": down, "up": up},
            "proxied": {"down": p_down, "up": p_up},
            "direct": {"down": d_down, "up": d_up},
            "coverage": {"core_down": core_down, "seen_down": seen_down,
                         "core_up": core_up, "seen_up": seen_up},
            "sections": {k: [{"key": r[0], "down": r[1], "up": r[2]} for r in v]
                         for k, v in sections.items()},
        }, ensure_ascii=False, indent=2))
        return 0

    window = args.since
    label = "%.0fh" % (window / 3600) if window >= 3600 else "%.0fm" % (window / 60)
    print("window: last %s (from %s)" % (label, time.strftime("%Y-%m-%d %H:%M", time.localtime(since))))
    if args.host or args.process:
        print("filter: %s" % " ".join(filter(None, [args.host, args.process])))

    total_all = (p_down + p_up) + (d_down + d_up)
    print("\nplan traffic (via a proxy node) : down %10s   up %10s" % (human(p_down), human(p_up)))
    print("direct (no plan cost)           : down %10s   up %10s" % (human(d_down), human(d_up)))
    if total_all:
        print("plan share of all traffic       : %.1f%%" % (100.0 * (p_down + p_up) / total_all))

    print_table("top processes [%s]" % args.scope, sections["process"])
    print_table("top hosts [%s]" % args.scope, sections["host"])
    print_table("top outbound nodes [%s]" % args.scope, sections["outbound"], width=28)
    print_table("top matching rules [%s]" % args.scope, sections["rule"], width=28)
    print("\n  a large share under 'Match' means the traffic hit no specific rule")
    print("  and fell through to the final catch-all.")

    if core_down:
        print("\ncoverage: attributed %s of %s the core counted (%.1f%% down)"
              % (human(seen_down), human(core_down), 100.0 * seen_down / core_down))
        print("  the gap is bytes moved between the last poll and a connection closing.")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
