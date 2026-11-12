# clash-traffic

Trace which process, host and node is spending your Clash subscription quota.

In rule mode a Clash client decides per connection whether it leaves through
the subscription or goes out directly. The operating system's own counters
see one network interface and cannot tell the two apart, so "my quota is
draining" is not a question Task Manager or a router graph can answer. This
tool reads the mihomo API, attributes every byte to a process, a hostname, an
outbound node and the rule that picked it, and stores the result locally.

Everything stays on the machine. There is no server, no telemetry, and no
network dependency beyond the local control channel.

## How attribution works

`GET /connections` is a snapshot of *live* connections. A connection
disappears from it the moment it closes and nothing is persisted by the
client, so a recorder has to be running to see anything at all.

* `collector.py` polls the endpoint and diffs consecutive snapshots by
  connection id. Byte deltas are booked into the minute they were *observed*,
  so a connection that stays open for hours is spread across the timeline
  rather than landing entirely in the minute it closed.
* `chains[0]` is the node a connection actually left through, and it is the
  only field that decides whether the connection cost subscription traffic.
  `chains[-1]` is the policy group that selected the node; it says nothing
  about where the bytes went. Local outbounds (`DIRECT`, `REJECT`,
  `REJECT-DROP`, `PASS`, `COMPATIBLE`, `BLACKHOLE`) are recorded but marked
  as free.
* Every minute the collector also stores what it attributed next to the
  core's own `uploadTotal` / `downloadTotal`. Bytes that move between the last
  poll and a connection closing cannot be observed, so the gap between the two
  numbers is a *measured* blind spot instead of an assumption. Across
  eighteen days of uninterrupted recording here the figure stayed between
  95% and 99.7%, with a median of 98.9%. It only means anything over a
  window long enough to dwarf a restart: the collector takes its baseline
  from the core counters when it starts, so whatever was already in flight
  at that moment is never attributed, and in a short window carrying little
  traffic that one gap alone can drag the ratio into the eighties.

Client-side byte counts will never match a provider's billing page exactly:
TLS and protocol overhead are counted differently, and nodes sold with a
multiplier are billed at that multiple of the bytes on the wire.

## Requirements

* Windows, with a Clash client that exposes the mihomo API. Clash Verge Rev
  launches the core with a named pipe (`\\.\pipe\verge-mihomo`) as the only
  control channel, which is the default transport here. A TCP
  `external-controller` works too, via `--api`.
* Two core options have to be on, or attribution degrades: process names come
  back empty and every host shows up as a bare IP address.

  ```yaml
  # Clash Verge Rev: Profiles -> Global Merge (Merge.yaml)
  find-process-mode: always

  # Recover the real domain for connections that go out to a bare IP, so
  # they can be grouped by host instead of by address. override-destination
  # stays false: sniffing is used for labelling only and must not change
  # which rule a connection matches.
  sniffer:
    enable: true
    parse-pure-ip: true
    force-dns-mapping: true
    override-destination: false
    sniff:
      HTTP:
        ports: [80, 8080-8880]
      TLS:
        ports: [443, 8443]
      QUIC:
        ports: [443, 8443]
    skip-domain:
      - "+.push.apple.com"
  ```

* Running from source needs Python 3.8+ and `pywebview` for the window.
  Everything else is standard library. The prebuilt executable needs neither.

## Running it

The executable and the sources behave identically. With no arguments the
control window opens; with a subcommand the same binary acts as a command
line tool.

```
ClashTraffic.exe                     control window
ClashTraffic.exe collect             the recorder (the window spawns this)
ClashTraffic.exe live --watch        live rates, highest first
ClashTraffic.exe report --since 7d   text tables
ClashTraffic.exe dashboard --open    self-contained HTML report
ClashTraffic.exe status              one-screen summary
```

From source, substitute `python app.py`.

In the window, **Start recording** detaches a collector process and leaves it
running after the window closes; the dot and pid in the header reflect the
real process, not a flag. Stopping writes a sentinel file so the recorder
flushes and exits cleanly, and only terminates it if it refuses.

The ranges are 1h / 24h / 7d, each compared against the immediately preceding
window of equal length. **Live** switches the panel to one-second sampling: the
strip is a pulse whose height tracks instantaneous throughput and fades over
roughly two seconds, with a rate axis so a bar cannot be mistaken for the
wrong order of magnitude. Colour follows the connection rather than its row
number, so a process keeps its colour when the list reorders.

Useful flags:

```
collect    --interval 1.0 --commit-every 15 --db PATH --api HOST:PORT --secret S --once
           --keep-conn DAYS --keep-agg DAYS --prune
live       --watch --interval 3 --limit 20 --threshold 1.0
report     --since 24h --scope proxied|direct|all --limit 15 --host H --process P --json
dashboard  --out PATH --open
```

## Data

A single SQLite file, `traffic.db`, next to the executable (WAL mode).

| Table | One row per | Holds |
|---|---|---|
| `minute_agg` | minute x process x host x outbound x rule | `up`, `down`, `is_proxied` |
| `conn` | finished connection | timestamps, process path, destination, chain, rule, totals |
| `recon` | minute | `core_up`/`core_down` against `seen_up`/`seen_down`, poll count |

`minute_agg` answers every aggregate question and stays small. `conn` is there
for the "what exactly was that one transfer" case. `recon` is how the coverage
figure above is derived rather than guessed.

Of the three, only `conn` grows in proportion to activity: it keeps a row per
connection carrying the full process path and policy chain, while `minute_agg`
collapses a whole minute into one row per process and host. Retention has a
separate window per table and both default to keeping everything, because
discarding recorded history should be a decision rather than a side effect of
running a newer build.

```
collect --keep-conn 60              trim while recording
collect --keep-conn 60 --prune      trim an existing file now, then exit
```

The recorder started from the window carries `--keep-conn 90`; the value is
`KEEP_CONN_DAYS` in `app.py`.

A one-shot prune rebuilds the file afterwards, since deleted pages otherwise
stay in it as free space and shrinking it is the point.

The database is excluded from version control on purpose: it records every
host the machine contacted, which is browsing history.

## Building

```powershell
.\build.ps1           # single-file executable next to the sources
.\build.ps1 -Icon     # regenerate the icon first
```

PyInstaller output goes to a temporary directory so the working tree keeps
only the finished binary. `assets/make-icon.py` draws the multi-size `.ico`
with no third-party dependency: a distance field with 4x supersampling,
hand-assembled PNG and ICO containers, and a per-size stroke weight because
bilinear downscaling destroys thin strokes at 16 px.

## Layout

```
app.py          entry point, process control, command dispatch, fallback window
ui.py           native frameless window, API bridge, range queries
ui.html         the interface
collector.py    polling, snapshot diffing, SQLite writes
mihomo_api.py   named pipe and TCP transports for the mihomo API
dashboard.py    self-contained HTML report, hand-drawn SVG, no CDN
report.py       text tables
live.py         live rate sampling
status.py       summary
paths.py        frozen-aware path resolution
```

## License

MIT
