"""Show which connections are moving bytes right now.

Takes two snapshots of /connections a short interval apart and ranks the
difference, so the output is current throughput rather than the lifetime
totals /connections reports.

Watch mode keeps sampling and prints the top talkers only when throughput
crosses a threshold, which is how an intermittent burst gets caught without
having to be at the keyboard when it starts.

Usage:
    python live.py                      sample once over 3 seconds
    python live.py -i 5 -n 25
    python live.py --watch              alert whenever down exceeds 1 MB/s
    python live.py --watch -t 0.3       lower the threshold to 300 KB/s
"""

import argparse
import sys
import time

from mihomo_api import DEFAULT_PIPE, ApiError, build_client
from collector import LOCAL_OUTBOUNDS, describe, human


def snapshot(client):
    data = client.get_json("/connections")
    live = {}
    for conn in data.get("connections") or []:
        cid = conn.get("id")
        if cid:
            live[cid] = (
                int(conn.get("upload") or 0),
                int(conn.get("download") or 0),
                describe(conn),
            )
    return live, int(data.get("uploadTotal") or 0), int(data.get("downloadTotal") or 0)


def rates(before, after, elapsed):
    rows = []
    for cid, (up, down, info) in after.items():
        prev = before.get(cid)
        # A connection opened during the sample counts all of its bytes.
        d_up = up - prev[0] if prev else up
        d_down = down - prev[1] if prev else down
        if d_up > 0 or d_down > 0:
            rows.append((d_down / elapsed, d_up / elapsed, info))
    rows.sort(key=lambda r: -(r[0] + r[1]))
    return rows


def print_rows(rows, limit):
    print("%12s %12s  %-20s %-40s %-14s %s"
          % ("DOWN/s", "UP/s", "PROCESS", "HOST", "OUTBOUND", "RULE"))
    print("-" * 132)
    for d_down, d_up, info in rows[:limit]:
        cost = "  *" if info["outbound"] not in LOCAL_OUTBOUNDS else ""
        print("%12s %12s  %-20s %-40s %-14s %s%s"
              % (human(d_down) + "/s", human(d_up) + "/s",
                 info["process"][:20], info["host"][:40], info["outbound"][:14],
                 (info["rule"] + ":" + info["rule_payload"])[:22], cost))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pipe", default=DEFAULT_PIPE)
    parser.add_argument("--api")
    parser.add_argument("--secret")
    parser.add_argument("-i", "--interval", type=float, default=3.0)
    parser.add_argument("-n", "--limit", type=int, default=20)
    parser.add_argument("--watch", action="store_true",
                        help="keep sampling and report only when busy")
    parser.add_argument("-t", "--threshold", type=float, default=1.0,
                        help="watch mode: MB/s of download that triggers a report")
    args = parser.parse_args(argv)

    client = build_client(args)
    trigger = args.threshold * 1024 * 1024

    if not args.watch:
        before, up0, down0 = snapshot(client)
        started = time.time()
        time.sleep(args.interval)
        after, up1, down1 = snapshot(client)
        elapsed = time.time() - started
        rows = rates(before, after, elapsed)

        print("sampled %.1fs -- core is moving %s/s down, %s/s up across %d live connections"
              % (elapsed, human((down1 - down0) / elapsed),
                 human((up1 - up0) / elapsed), len(after)))
        print("accounted for below: %s/s down\n" % human(sum(r[0] for r in rows)))
        print_rows(rows, args.limit)
        print("\n  * = left through a proxy node, so it spends subscription quota")
        return 0

    # Watch mode is long-running and often piped to a file, where the default
    # block buffering would hold output back for minutes.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    print("watching, reports when download exceeds %s/s -- Ctrl-C to stop"
          % human(trigger))
    before, _, down0 = snapshot(client)
    last = time.time()
    quiet_since = None
    try:
        while True:
            time.sleep(args.interval)
            try:
                after, _, down1 = snapshot(client)
            except ApiError as exc:
                print("api unavailable: %s" % exc, file=sys.stderr)
                time.sleep(2.0)
                continue
            now = time.time()
            elapsed = now - last
            rate = (down1 - down0) / elapsed if elapsed > 0 else 0.0

            if rate >= trigger:
                print("\n=== %s  %s/s down ===" % (time.strftime("%H:%M:%S"), human(rate)))
                print_rows(rates(before, after, elapsed), args.limit)
                quiet_since = None
            elif quiet_since is None:
                quiet_since = now
            elif now - quiet_since >= 300:
                print("  %s  idle, %s/s" % (time.strftime("%H:%M:%S"), human(rate)))
                quiet_since = now

            before, down0, last = after, down1, now
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
