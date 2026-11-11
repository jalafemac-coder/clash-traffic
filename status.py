"""Print a short summary of what the database currently holds.

Used by the monitor menu to show state without duplicating SQL in PowerShell,
which has no sqlite of its own.
"""

import os
import sqlite3
import sys
import time

import paths

DB = paths.DB


def human(n):
    value = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return "%.2f %s" % (value, unit)
        value /= 1024


def main():
    if not os.path.exists(DB):
        print("  Database : not created yet")
        return 0

    size = os.path.getsize(DB)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(DB + suffix):
            size += os.path.getsize(DB + suffix)

    db = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    minutes, first, last = db.execute(
        "SELECT COUNT(DISTINCT minute_ts), MIN(minute_ts), MAX(minute_ts) FROM minute_agg"
    ).fetchone()
    conns = db.execute("SELECT COUNT(*) FROM conn").fetchone()[0]

    day = int(time.time()) - 86400
    proxied, direct = db.execute(
        "SELECT COALESCE(SUM(CASE WHEN is_proxied=1 THEN down+up END),0),"
        "       COALESCE(SUM(CASE WHEN is_proxied=0 THEN down+up END),0)"
        " FROM minute_agg WHERE minute_ts >= ?", (day,)
    ).fetchone()

    print("  Database : %s, %s minutes recorded, %s connections"
          % (human(size), minutes or 0, conns))
    if first:
        print("  Covering : %s  ->  %s"
              % (time.strftime("%m-%d %H:%M", time.localtime(first)),
                 time.strftime("%m-%d %H:%M", time.localtime(last))))
    print("  Last 24h : %s via proxy (plan traffic), %s direct"
          % (human(proxied), human(direct)))

    top = db.execute(
        "SELECT process, SUM(down+up) AS t FROM minute_agg"
        " WHERE minute_ts >= ? AND is_proxied = 1"
        " GROUP BY process ORDER BY t DESC LIMIT 3", (day,)
    ).fetchall()
    if top:
        print("  Top plan : " + ", ".join("%s %s" % (p, human(t)) for p, t in top))
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
