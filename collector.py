"""Attribute proxy traffic to processes, hosts, nodes and rules.

/connections is a snapshot of live connections only: a connection vanishes
from it the moment it closes, and nothing is persisted by the client. This
collector polls the endpoint, diffs consecutive snapshots by connection id,
and stores the result in SQLite.

Three tables are written:

  minute_agg  Per-minute byte deltas. Because deltas are booked in the minute
              they were observed, a connection that stays open for hours is
              spread across the timeline instead of landing entirely in the
              minute it closed.
  conn        One row per finished connection, for per-connection detail.
  recon       Per minute, what this collector attributed next to the core's
              own uploadTotal/downloadTotal counters. Bytes moved between the
              last poll and a connection closing cannot be observed, so the
              gap between the two is the measured blind spot rather than an
              assumption.

Whether a connection spends subscription traffic is decided by chains[0],
the node the connection actually left through. chains[-1] is the policy
group that selected it.

Usage:
    python collector.py                     start collecting
    python collector.py --once              single poll, print and exit
    python collector.py --db D:/other.db --interval 2
"""

import argparse
import os
import signal
import sqlite3
import sys
import time

import paths
from mihomo_api import DEFAULT_PIPE, ApiError, build_client, parse_ts

# chains[0] values that mean the connection never left through the
# subscription and therefore costs no plan traffic.
LOCAL_OUTBOUNDS = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE", "BLACKHOLE"}

UNKNOWN_PROCESS = "(unknown)"

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS minute_agg (
    minute_ts  INTEGER NOT NULL,
    process    TEXT    NOT NULL,
    host       TEXT    NOT NULL,
    outbound   TEXT    NOT NULL,
    rule       TEXT    NOT NULL,
    is_proxied INTEGER NOT NULL,
    up         INTEGER NOT NULL,
    down       INTEGER NOT NULL,
    PRIMARY KEY (minute_ts, process, host, outbound, rule, is_proxied)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_agg_minute  ON minute_agg (minute_ts);
CREATE INDEX IF NOT EXISTS idx_agg_process ON minute_agg (process);
CREATE INDEX IF NOT EXISTS idx_agg_host    ON minute_agg (host);

CREATE TABLE IF NOT EXISTS conn (
    epoch        TEXT    NOT NULL,
    conn_id      TEXT    NOT NULL,
    start_ts     REAL,
    end_ts       REAL    NOT NULL,
    process      TEXT    NOT NULL,
    process_path TEXT,
    host         TEXT    NOT NULL,
    dest_ip      TEXT,
    dest_port    TEXT,
    network      TEXT,
    conn_type    TEXT,
    outbound     TEXT    NOT NULL,
    chain        TEXT,
    is_proxied   INTEGER NOT NULL,
    rule         TEXT,
    rule_payload TEXT,
    up           INTEGER NOT NULL,
    down         INTEGER NOT NULL,
    PRIMARY KEY (epoch, conn_id)
);

CREATE INDEX IF NOT EXISTS idx_conn_end     ON conn (end_ts);
CREATE INDEX IF NOT EXISTS idx_conn_proxied ON conn (is_proxied, down);

CREATE TABLE IF NOT EXISTS recon (
    minute_ts INTEGER PRIMARY KEY,
    core_up   INTEGER NOT NULL,
    core_down INTEGER NOT NULL,
    seen_up   INTEGER NOT NULL,
    seen_down INTEGER NOT NULL,
    polls     INTEGER NOT NULL
);
"""


def open_db(path):
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    db = sqlite3.connect(path, isolation_level=None)
    db.executescript(SCHEMA)
    return db


def describe(conn):
    """Reduce a raw connection record to the fields traffic is grouped by."""
    meta = conn.get("metadata") or {}
    chains = conn.get("chains") or []

    outbound = chains[0] if chains else "UNKNOWN"
    host = meta.get("host") or meta.get("sniffHost") or meta.get("destinationIP") or ""
    if not host:
        host = meta.get("remoteDestination") or "(no host)"

    process = meta.get("process") or ""
    if not process:
        path = meta.get("processPath") or ""
        process = os.path.basename(path.replace("\\", "/")) if path else UNKNOWN_PROCESS

    return {
        "process": process or UNKNOWN_PROCESS,
        "process_path": meta.get("processPath") or "",
        "host": host,
        "dest_ip": meta.get("destinationIP") or meta.get("remoteDestination") or "",
        "dest_port": meta.get("destinationPort") or "",
        "network": meta.get("network") or "",
        "conn_type": meta.get("type") or "",
        "outbound": outbound,
        "chain": " <- ".join(chains),
        "is_proxied": 0 if outbound in LOCAL_OUTBOUNDS else 1,
        "rule": conn.get("rule") or "",
        "rule_payload": conn.get("rulePayload") or "",
    }


class Collector:
    def __init__(self, client, db, verbose=False):
        self.client = client
        self.db = db
        self.verbose = verbose

        self.live = {}            # conn_id -> last observed counters and metadata
        self.buckets = {}         # (minute, process, host, outbound, rule, proxied) -> [up, down]
        self.recon = {}           # minute -> [core_up, core_down, seen_up, seen_down, polls]
        self.finished = []        # conn rows waiting to be written

        self.prev_totals = None
        self.epoch = None
        self.seeded = False       # False until the first snapshot has been baselined
        self.polls = 0

    # A restart of the core resets its counters and starts handing out
    # connection ids again, so rows are namespaced by an epoch.
    def _new_epoch(self):
        self.epoch = time.strftime("%Y%m%dT%H%M%S", time.localtime())
        self.live.clear()
        self.seeded = False

    def poll(self):
        data = self.client.get_json("/connections")
        now = time.time()
        minute = int(now // 60 * 60)
        conns = data.get("connections") or []

        core_up = int(data.get("uploadTotal") or 0)
        core_down = int(data.get("downloadTotal") or 0)

        if self.epoch is None:
            self._new_epoch()
        elif self.prev_totals and (
            core_up < self.prev_totals[0] or core_down < self.prev_totals[1]
        ):
            # Counters only ever move up within one core lifetime.
            self._flush_finished(now)
            self._new_epoch()

        d_core_up = d_core_down = 0
        if self.prev_totals and self.seeded:
            d_core_up = max(0, core_up - self.prev_totals[0])
            d_core_down = max(0, core_down - self.prev_totals[1])
        self.prev_totals = (core_up, core_down)

        seen_up = seen_down = 0
        current_ids = set()

        for conn in conns:
            cid = conn.get("id")
            if not cid:
                continue
            current_ids.add(cid)

            up = int(conn.get("upload") or 0)
            down = int(conn.get("download") or 0)
            info = describe(conn)

            prev = self.live.get(cid)
            if prev is None:
                # A connection already running when this collector attached
                # carries bytes that were never observed here. Baseline it so
                # those bytes are not booked to the current minute.
                d_up, d_down = (up, down) if self.seeded else (0, 0)
                self.live[cid] = {
                    "up": up,
                    "down": down,
                    "start_ts": parse_ts(conn.get("start")),
                    "info": info,
                }
            else:
                d_up = max(0, up - prev["up"])
                d_down = max(0, down - prev["down"])
                prev["up"] = up
                prev["down"] = down
                # Metadata is not final at connect time: a sniffed host and
                # the owning process can both arrive on a later snapshot.
                prev["info"] = info

            if d_up or d_down:
                key = (
                    minute,
                    info["process"],
                    info["host"],
                    info["outbound"],
                    info["rule"],
                    info["is_proxied"],
                )
                slot = self.buckets.get(key)
                if slot is None:
                    self.buckets[key] = [d_up, d_down]
                else:
                    slot[0] += d_up
                    slot[1] += d_down
                seen_up += d_up
                seen_down += d_down

        for cid in set(self.live) - current_ids:
            self._retire(cid, now)

        slot = self.recon.get(minute)
        if slot is None:
            self.recon[minute] = [d_core_up, d_core_down, seen_up, seen_down, 1]
        else:
            slot[0] += d_core_up
            slot[1] += d_core_down
            slot[2] += seen_up
            slot[3] += seen_down
            slot[4] += 1

        self.seeded = True
        self.polls += 1
        return len(conns), seen_down

    def _retire(self, cid, now):
        rec = self.live.pop(cid)
        info = rec["info"]
        self.finished.append(
            (
                self.epoch,
                cid,
                rec["start_ts"],
                now,
                info["process"],
                info["process_path"],
                info["host"],
                info["dest_ip"],
                info["dest_port"],
                info["network"],
                info["conn_type"],
                info["outbound"],
                info["chain"],
                info["is_proxied"],
                info["rule"],
                info["rule_payload"],
                rec["up"],
                rec["down"],
            )
        )

    def _flush_finished(self, now):
        for cid in list(self.live):
            self._retire(cid, now)

    def flush(self):
        if not (self.buckets or self.recon or self.finished):
            return
        cur = self.db.cursor()
        cur.execute("BEGIN")
        try:
            if self.buckets:
                cur.executemany(
                    "INSERT INTO minute_agg"
                    " (minute_ts, process, host, outbound, rule, is_proxied, up, down)"
                    " VALUES (?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(minute_ts, process, host, outbound, rule, is_proxied)"
                    " DO UPDATE SET up = up + excluded.up, down = down + excluded.down",
                    [k + tuple(v) for k, v in self.buckets.items()],
                )
            if self.recon:
                cur.executemany(
                    "INSERT INTO recon"
                    " (minute_ts, core_up, core_down, seen_up, seen_down, polls)"
                    " VALUES (?,?,?,?,?,?)"
                    " ON CONFLICT(minute_ts) DO UPDATE SET"
                    "   core_up = core_up + excluded.core_up,"
                    "   core_down = core_down + excluded.core_down,"
                    "   seen_up = seen_up + excluded.seen_up,"
                    "   seen_down = seen_down + excluded.seen_down,"
                    "   polls = polls + excluded.polls",
                    [(k,) + tuple(v) for k, v in self.recon.items()],
                )
            if self.finished:
                cur.executemany(
                    "INSERT INTO conn"
                    " (epoch, conn_id, start_ts, end_ts, process, process_path, host,"
                    "  dest_ip, dest_port, network, conn_type, outbound, chain,"
                    "  is_proxied, rule, rule_payload, up, down)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(epoch, conn_id) DO UPDATE SET"
                    "   end_ts = excluded.end_ts,"
                    "   up = excluded.up, down = excluded.down",
                    self.finished,
                )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        self.buckets.clear()
        self.recon.clear()
        del self.finished[:]


def human(n):
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return "%.1f %s" % (value, unit)
        value /= 1024


def run_once(client):
    data = client.get_json("/connections")
    conns = data.get("connections") or []
    rows = []
    for conn in conns:
        info = describe(conn)
        rows.append((int(conn.get("download") or 0), info))
    rows.sort(key=lambda r: -r[0])

    print("core totals: down %s / up %s, %d live connections\n" % (
        human(data.get("downloadTotal") or 0),
        human(data.get("uploadTotal") or 0),
        len(conns),
    ))
    print("%-22s %-42s %-14s %-16s %10s" % ("PROCESS", "HOST", "OUTBOUND", "RULE", "DOWN"))
    for down, info in rows[:40]:
        print("%-22s %-42s %-14s %-16s %10s" % (
            info["process"][:22],
            info["host"][:42],
            info["outbound"][:14],
            (info["rule"] + ":" + info["rule_payload"])[:16],
            human(down),
        ))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=paths.DB)
    parser.add_argument("--pipe", default=DEFAULT_PIPE, help="mihomo named pipe")
    parser.add_argument("--api", help="TCP external-controller, e.g. 127.0.0.1:9097")
    parser.add_argument("--secret", help="external-controller secret")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between polls")
    parser.add_argument("--commit-every", type=float, default=15.0, help="seconds between DB writes")
    parser.add_argument("--once", action="store_true", help="print one snapshot and exit")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    client = build_client(args)

    if args.once:
        run_once(client)
        return 0

    db = open_db(args.db)
    collector = Collector(client, db)

    stopping = {"flag": False}

    def stop(_signum, _frame):
        stopping["flag"] = True

    signal.signal(signal.SIGINT, stop)
    try:
        signal.signal(signal.SIGTERM, stop)
    except (AttributeError, ValueError):
        pass

    if not args.quiet:
        print("collecting into %s (interval %.1fs), Ctrl-C to stop"
              % (os.path.abspath(args.db), args.interval))

    # A console-less collector cannot be sent Ctrl-C, so the toggle asks it to
    # stop by dropping this file next to the database. Stopping this way lets
    # the last buffered minute reach disk instead of being killed mid-poll.
    stop_path = os.path.join(os.path.dirname(os.path.abspath(args.db)), "collector.stop")
    if os.path.exists(stop_path):
        os.remove(stop_path)

    # The front end holds no handle on this process, so liveness is published
    # here: the file carries the pid and its mtime is refreshed on every
    # commit. A stale mtime means the process died without cleaning up, which
    # a bare pid cannot express once the number has been reused.
    pid_path = os.path.join(os.path.dirname(os.path.abspath(args.db)), "collector.pid")
    with open(pid_path, "w") as fh:
        fh.write(str(os.getpid()))

    last_commit = time.time()
    errors = 0
    while not stopping["flag"]:
        if os.path.exists(stop_path):
            os.remove(stop_path)
            break
        started = time.time()
        try:
            live, _ = collector.poll()
            errors = 0
        except ApiError as exc:
            errors += 1
            if errors in (1, 10) or errors % 60 == 0:
                print("api unavailable (%d in a row): %s" % (errors, exc), file=sys.stderr)
            live = 0
        except Exception as exc:  # keep the collector alive across transient faults
            errors += 1
            print("poll failed: %r" % (exc,), file=sys.stderr)
            live = 0

        now = time.time()
        if now - last_commit >= args.commit_every:
            collector.flush()
            last_commit = now
            try:
                os.utime(pid_path, None)
            except OSError:
                pass
            if not args.quiet:
                print("\r%s  live=%-4d polls=%-7d" % (
                    time.strftime("%H:%M:%S"), live, collector.polls), end="", flush=True)

        delay = args.interval - (time.time() - started)
        if delay > 0:
            time.sleep(delay)

    collector._flush_finished(time.time())
    collector.flush()
    db.close()
    try:
        os.remove(pid_path)
    except OSError:
        pass
    if not args.quiet:
        print("\nstopped after %d polls" % collector.polls)
    return 0


if __name__ == "__main__":
    sys.exit(main())
