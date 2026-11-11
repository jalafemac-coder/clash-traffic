"""Render the recorded traffic as a self-contained HTML dashboard.

Everything is inlined -- no server, no network, no chart library -- so the
output is a single file that opens straight from disk and keeps working
offline. Charts are hand-drawn SVG built from the same payload the table view
reads, so no value is reachable only by hovering.

Usage:
    python dashboard.py                 write dashboard.html next to the db
    python dashboard.py --open          write it and open it in the browser
    python dashboard.py -o D:/t.html
"""

import argparse
import json
import os
import sqlite3
import sys
import time
import webbrowser

import paths

HERE = paths.BASE
DEFAULT_DB = paths.DB
DEFAULT_OUT = paths.DASHBOARD

# label, span in seconds, bucket width in seconds
WINDOWS = [
    ("1h", 3600, 60),
    ("6h", 21600, 300),
    ("24h", 86400, 900),
    ("7d", 604800, 3600),
]

TIMELINE_SERIES = 4   # top processes drawn separately; the tail folds into Other
RANK_ROWS = 8


def human(n):
    value = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return "%.2f %s" % (value, unit)
        value /= 1024


def collect(db, span, bucket, now):
    since = int((now - span) // bucket * bucket)

    proxied_down, proxied_up, direct_down, direct_up = db.execute(
        "SELECT COALESCE(SUM(CASE WHEN is_proxied=1 THEN down END),0),"
        "       COALESCE(SUM(CASE WHEN is_proxied=1 THEN up   END),0),"
        "       COALESCE(SUM(CASE WHEN is_proxied=0 THEN down END),0),"
        "       COALESCE(SUM(CASE WHEN is_proxied=0 THEN up   END),0)"
        " FROM minute_agg WHERE minute_ts >= ?", (since,)
    ).fetchone()

    core_down, seen_down = db.execute(
        "SELECT COALESCE(SUM(core_down),0), COALESCE(SUM(seen_down),0)"
        " FROM recon WHERE minute_ts >= ?", (since,)
    ).fetchone()

    def rank(column, limit=RANK_ROWS):
        rows = db.execute(
            "SELECT %s AS k, SUM(down) AS d, SUM(up) AS u FROM minute_agg"
            " WHERE minute_ts >= ? AND is_proxied = 1"
            " GROUP BY k ORDER BY d + u DESC LIMIT ?" % column, (since, limit)
        ).fetchall()
        return [{"key": r[0] or "(empty)", "down": r[1], "up": r[2]} for r in rows]

    processes = rank("process")
    named = [p["key"] for p in processes[:TIMELINE_SERIES]]

    # Timeline is plan traffic only: it is the spend the tool exists to explain.
    buckets = {}
    for minute, process, down, up in db.execute(
        "SELECT minute_ts, process, SUM(down), SUM(up) FROM minute_agg"
        " WHERE minute_ts >= ? AND is_proxied = 1"
        " GROUP BY minute_ts, process", (since,)
    ):
        slot = int(minute // bucket * bucket)
        key = process if process in named else "Other"
        buckets.setdefault(slot, {})[key] = buckets.setdefault(slot, {}).get(key, 0) + down + up

    series = named + (["Other"] if any("Other" in v for v in buckets.values()) else [])
    points = []
    slot = since
    end = int(now // bucket * bucket)
    while slot <= end:
        values = buckets.get(slot, {})
        points.append({"t": slot, "v": [values.get(name, 0) for name in series]})
        slot += bucket

    return {
        "since": since,
        "bucket": bucket,
        "proxied": {"down": proxied_down, "up": proxied_up},
        "direct": {"down": direct_down, "up": direct_up},
        "coverage": {"core": core_down, "seen": seen_down},
        "series": series,
        "points": points,
        "processes": processes,
        "hosts": rank("host"),
        "rules": rank("rule"),
        "outbounds": rank("outbound", 6),
    }


def build(db_path):
    db = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
    now = time.time()
    payload = {
        "generated": now,
        "windows": [w[0] for w in WINDOWS],
        "data": {label: collect(db, span, bucket, now) for label, span, bucket in WINDOWS},
    }
    db.close()
    return payload


TEMPLATE = r"""<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clash traffic</title>
<style>
:root {
  color-scheme: light;
  --plane:      #f9f9f7;
  --surface:    #fcfcfb;
  --ink:        #0b0b0b;
  --ink-2:      #52514e;
  --muted:      #898781;
  --grid:       #e1e0d9;
  --axis:       #c3c2b7;
  --hairline:   rgba(11,11,11,0.10);
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --s4: #eda100; --s5: #e87ba4;
  --dim: #c3c2b7;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --plane:    #0d0d0d;
    --surface:  #1a1a19;
    --ink:      #ffffff;
    --ink-2:    #c3c2b7;
    --muted:    #898781;
    --grid:     #2c2c2a;
    --axis:     #383835;
    --hairline: rgba(255,255,255,0.10);
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500; --s5: #d55181;
    --dim: #4a4a46;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --plane:    #0d0d0d;
  --surface:  #1a1a19;
  --ink:      #ffffff;
  --ink-2:    #c3c2b7;
  --muted:    #898781;
  --grid:     #2c2c2a;
  --axis:     #383835;
  --hairline: rgba(255,255,255,0.10);
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500; --s5: #d55181;
  --dim: #4a4a46;
}

* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 24px 64px;
  background: var(--plane); color: var(--ink);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 1120px; margin: 0 auto; }
h1 { font-size: 20px; font-weight: 600; margin: 0 0 4px; letter-spacing: -0.01em; }
.sub { color: var(--muted); font-size: 13px; margin: 0 0 24px; }

.filters { display: flex; gap: 8px; align-items: center; margin: 0 0 24px; flex-wrap: wrap; }
.filters .lbl { color: var(--muted); font-size: 12px; margin-right: 4px; }
button.win {
  font: inherit; font-size: 13px; padding: 5px 14px; cursor: pointer;
  background: var(--surface); color: var(--ink-2);
  border: 1px solid var(--hairline); border-radius: 999px;
}
button.win[aria-pressed="true"] { background: var(--s1); color: #fff; border-color: transparent; }

.hero { background: var(--surface); border: 1px solid var(--hairline); border-radius: 12px;
        padding: 22px 24px; margin-bottom: 16px; }
.hero .label { color: var(--muted); font-size: 13px; }
.hero .value { font-size: 52px; font-weight: 600; line-height: 1.1; letter-spacing: -0.02em; margin-top: 2px; }
.hero .note { color: var(--ink-2); font-size: 13px; margin-top: 6px; }

.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 16px; margin-bottom: 16px; }
.tile { background: var(--surface); border: 1px solid var(--hairline); border-radius: 12px; padding: 16px 18px; }
.tile .label { color: var(--muted); font-size: 12px; }
.tile .value { font-size: 24px; font-weight: 600; margin-top: 2px; letter-spacing: -0.01em; }
.tile .note  { color: var(--muted); font-size: 12px; margin-top: 2px; }

.card { background: var(--surface); border: 1px solid var(--hairline); border-radius: 12px;
        padding: 18px 20px 14px; margin-bottom: 16px; }
.card header { display: flex; align-items: baseline; gap: 12px; margin-bottom: 2px; }
.card h2 { font-size: 14px; font-weight: 600; margin: 0; }
.card .cap { color: var(--muted); font-size: 12px; margin: 0 0 14px; }
.card .tbtn { margin-left: auto; font: inherit; font-size: 12px; color: var(--ink-2);
              background: none; border: 1px solid var(--hairline); border-radius: 6px;
              padding: 3px 10px; cursor: pointer; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 16px; }

.legend { display: flex; flex-wrap: wrap; gap: 14px; margin: 0 0 10px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; color: var(--ink-2); font-size: 12px; }
.legend i { width: 10px; height: 10px; border-radius: 3px; display: block; }

.plot { width: 100%; overflow-x: auto; }
svg { display: block; max-width: 100%; height: auto; }
.gridline { stroke: var(--grid); stroke-width: 1; }
.axisline { stroke: var(--axis); stroke-width: 1; }
.tick { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.barlabel { fill: var(--ink-2); font-size: 11px; font-variant-numeric: tabular-nums; }
.catlabel { fill: var(--ink-2); font-size: 12px; }

table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--hairline); }
th { color: var(--muted); font-weight: 500; font-size: 12px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.tablewrap { overflow-x: auto; }
.hidden { display: none; }

#tip {
  position: fixed; z-index: 9; pointer-events: none; opacity: 0;
  background: var(--surface); color: var(--ink);
  border: 1px solid var(--hairline); border-radius: 8px;
  padding: 8px 10px; font-size: 12px; line-height: 1.45;
  box-shadow: 0 6px 20px rgba(0,0,0,0.18); transition: opacity .08s; max-width: 280px;
}
#tip b { font-weight: 600; }
#tip .row { display: flex; align-items: center; gap: 6px; white-space: nowrap; }
#tip i { width: 8px; height: 8px; border-radius: 2px; display: block; flex: none; }
#tip .v { margin-left: auto; font-variant-numeric: tabular-nums; padding-left: 12px; }
.empty { color: var(--muted); font-size: 13px; padding: 24px 0; text-align: center; }
</style>

<div class="wrap">
  <h1>Clash traffic</h1>
  <p class="sub" id="sub"></p>

  <div class="filters">
    <span class="lbl">Window</span>
    <span id="winbtns"></span>
  </div>

  <div class="hero">
    <div class="label">Plan traffic — left through a proxy node</div>
    <div class="value" id="heroval">—</div>
    <div class="note" id="heronote"></div>
  </div>

  <div class="kpis" id="kpis"></div>

  <div class="card">
    <header>
      <h2>Plan traffic over time</h2>
      <button class="tbtn" data-table="timeline">Table</button>
    </header>
    <p class="cap" id="timelinecap"></p>
    <div class="legend" id="tlegend"></div>
    <div class="plot" id="timeline"></div>
    <div class="tablewrap hidden" id="timeline_table"></div>
  </div>

  <div class="card">
    <header>
      <h2>Where it went</h2>
      <button class="tbtn" data-table="split">Table</button>
    </header>
    <p class="cap">Proxy traffic spends subscription quota. Direct traffic does not.</p>
    <div class="plot" id="split"></div>
    <div class="tablewrap hidden" id="split_table"></div>
  </div>

  <div class="grid2">
    <div class="card">
      <header><h2>Top processes</h2><button class="tbtn" data-table="processes">Table</button></header>
      <p class="cap">Plan traffic only. Colors match the timeline above.</p>
      <div class="plot" id="processes"></div>
      <div class="tablewrap hidden" id="processes_table"></div>
    </div>
    <div class="card">
      <header><h2>Top hosts</h2><button class="tbtn" data-table="hosts">Table</button></header>
      <p class="cap">Plan traffic only.</p>
      <div class="plot" id="hosts"></div>
      <div class="tablewrap hidden" id="hosts_table"></div>
    </div>
    <div class="card">
      <header><h2>Matching rule</h2><button class="tbtn" data-table="rules">Table</button></header>
      <p class="cap">Match is the catch-all: traffic that hit no specific rule.</p>
      <div class="plot" id="rules"></div>
      <div class="tablewrap hidden" id="rules_table"></div>
    </div>
    <div class="card">
      <header><h2>Outbound node</h2><button class="tbtn" data-table="outbounds">Table</button></header>
      <p class="cap">Plan traffic only.</p>
      <div class="plot" id="outbounds"></div>
      <div class="tablewrap hidden" id="outbounds_table"></div>
    </div>
  </div>
</div>
<div id="tip"></div>

<script>
const PAYLOAD = __PAYLOAD__;
const SERIES_VARS = ['--s1','--s2','--s3','--s4','--s5'];
const cssv = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
let current = '24h';

function human(n) {
  let v = Math.abs(Number(n) || 0);
  const u = ['B','KB','MB','GB','TB'];
  let i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 100 || i === 0 ? 0 : 2) + ' ' + u[i];
}
function clock(ts, bucket) {
  const d = new Date(ts * 1000);
  const p = x => String(x).padStart(2, '0');
  return bucket >= 3600 ? `${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:00`
                        : `${p(d.getHours())}:${p(d.getMinutes())}`;
}
// Axis ticks land on round numbers rather than peak/n, so the reader gets
// 0 / 250 MB / 500 MB instead of 0 / 231 MB / 462 MB. Stepping is done inside
// the binary unit the peak falls in, so the labels stay round after
// human() formats them.
function niceMax(peak, ticks) {
  if (!(peak > 0)) return 1;
  let unit = 1;
  while (peak / unit >= 1024 && unit < Math.pow(1024, 4)) unit *= 1024;
  const raw = peak / unit / ticks;
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / pow;
  const mult = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
  return mult * pow * unit * ticks;
}
function seriesColor(name, series) {
  if (name === 'Other') return cssv('--dim');
  const i = series.indexOf(name);
  return i >= 0 && i < SERIES_VARS.length ? cssv(SERIES_VARS[i]) : cssv('--dim');
}
const el = (t, a = {}) => {
  const n = document.createElementNS('http://www.w3.org/2000/svg', t);
  for (const k in a) n.setAttribute(k, a[k]);
  return n;
};
// Rounded on the data end, square where it meets the baseline.
function capPath(x, y, w, h, r) {
  r = Math.max(0, Math.min(r, w / 2, h));
  return `M${x},${y+h} L${x},${y+r} Q${x},${y} ${x+r},${y} L${x+w-r},${y} Q${x+w},${y} ${x+w},${y+r} L${x+w},${y+h} Z`;
}
function capPathH(x, y, w, h, r) {
  r = Math.max(0, Math.min(r, h / 2, w));
  return `M${x},${y} L${x+w-r},${y} Q${x+w},${y} ${x+w},${y+r} L${x+w},${y+h-r} Q${x+w},${y+h} ${x+w-r},${y+h} L${x},${y+h} Z`;
}

const tip = document.getElementById('tip');
function showTip(evt, html) {
  tip.innerHTML = html;
  tip.style.opacity = 1;
  const pad = 14, r = tip.getBoundingClientRect();
  let x = evt.clientX + pad, y = evt.clientY + pad;
  if (x + r.width > innerWidth - 8) x = evt.clientX - r.width - pad;
  if (y + r.height > innerHeight - 8) y = evt.clientY - r.height - pad;
  tip.style.left = Math.max(8, x) + 'px';
  tip.style.top = Math.max(8, y) + 'px';
}
const hideTip = () => { tip.style.opacity = 0; };

function drawTimeline(d) {
  const host = document.getElementById('timeline');
  host.innerHTML = '';
  const pts = d.points, series = d.series;
  const totals = pts.map(p => p.v.reduce((a, b) => a + b, 0));
  const peak = niceMax(Math.max(...totals, 1), 4);
  if (!series.length || !totals.some(t => t > 0)) {
    host.innerHTML = '<p class="empty">No plan traffic recorded in this window.</p>';
    return;
  }

  const W = Math.max(680, host.clientWidth || 680), H = 260;
  const m = { t: 12, r: 16, b: 30, l: 62 };
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: 'img' });

  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const y = m.t + ph - (ph * i / ticks);
    svg.appendChild(el('line', { x1: m.l, y1: y, x2: m.l + pw, y2: y, class: i ? 'gridline' : 'axisline' }));
    const lab = el('text', { x: m.l - 8, y: y + 4, class: 'tick', 'text-anchor': 'end' });
    lab.textContent = human(peak * i / ticks);
    svg.appendChild(lab);
  }

  const band = pw / pts.length;
  const bw = Math.min(24, Math.max(2, band - 2));
  const GAP = 2;

  pts.forEach((p, i) => {
    const x = m.l + band * i + (band - bw) / 2;
    let acc = 0;
    const stack = [];
    p.v.forEach((val, si) => { if (val > 0) stack.push([si, val]); });
    stack.forEach(([si, val], k) => {
      const h = (val / peak) * ph;
      const y = m.t + ph - ((acc + val) / peak) * ph;
      acc += val;
      const isTop = k === stack.length - 1;
      const gap = k === 0 ? 0 : GAP;
      const hh = Math.max(1, h - gap);
      const fill = seriesColor(series[si], series);
      svg.appendChild(isTop
        ? el('path', { d: capPath(x, y, bw, hh, 4), fill })
        : el('rect', { x, y, width: bw, height: hh, fill }));
    });
  });

  // Hit area spans the whole band so narrow columns stay easy to reach.
  pts.forEach((p, i) => {
    const hit = el('rect', {
      x: m.l + band * i, y: m.t, width: Math.max(band, 1), height: ph,
      fill: 'transparent', style: 'cursor:crosshair'
    });
    const total = totals[i];
    hit.addEventListener('mousemove', e => {
      let rows = `<b>${clock(p.t, d.bucket)}</b><div class="row"><span>Total</span><span class="v">${human(total)}</span></div>`;
      series.forEach((name, si) => {
        if (p.v[si] > 0) rows += `<div class="row"><i style="background:${seriesColor(name, series)}"></i><span>${name}</span><span class="v">${human(p.v[si])}</span></div>`;
      });
      showTip(e, rows);
    });
    hit.addEventListener('mouseleave', hideTip);
    svg.appendChild(hit);
  });

  const every = Math.max(1, Math.ceil(pts.length / 8));
  pts.forEach((p, i) => {
    if (i % every) return;
    const lab = el('text', { x: m.l + band * i + band / 2, y: H - 10, class: 'tick', 'text-anchor': 'middle' });
    lab.textContent = clock(p.t, d.bucket);
    svg.appendChild(lab);
  });

  host.appendChild(svg);

  const leg = document.getElementById('tlegend');
  leg.innerHTML = series.map(n =>
    `<span><i style="background:${seriesColor(n, series)}"></i>${n}</span>`).join('');
}

function drawRank(id, rows, colorFn, emphasis) {
  const host = document.getElementById(id);
  host.innerHTML = '';
  if (!rows.length) { host.innerHTML = '<p class="empty">Nothing recorded in this window.</p>'; return; }

  const vals = rows.map(r => r.down + r.up);
  const peak = Math.max(...vals, 1);
  const rowH = 30, barH = Math.min(18, rowH - 12);
  const W = Math.max(420, host.clientWidth || 420);
  const m = { t: 6, r: 88, b: 6, l: 148 };
  const H = m.t + m.b + rows.length * rowH;
  const pw = W - m.l - m.r;
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: 'img' });

  rows.forEach((r, i) => {
    const v = vals[i];
    const y = m.t + i * rowH + (rowH - barH) / 2;
    const w = Math.max(2, (v / peak) * pw);
    svg.appendChild(el('path', { d: capPathH(m.l, y, w, barH, 4), fill: colorFn(r, i) }));

    let name = r.key;
    if (name.length > 22) name = name.slice(0, 21) + '\u2026';
    const lab = el('text', { x: m.l - 10, y: y + barH / 2 + 4, class: 'catlabel', 'text-anchor': 'end' });
    lab.textContent = name;
    svg.appendChild(lab);

    const val = el('text', { x: m.l + w + 8, y: y + barH / 2 + 4, class: 'barlabel' });
    val.textContent = human(v);
    svg.appendChild(val);

    const hit = el('rect', { x: 0, y: m.t + i * rowH, width: W, height: rowH, fill: 'transparent' });
    hit.addEventListener('mousemove', e => showTip(e,
      `<b>${r.key}</b><div class="row"><span>Down</span><span class="v">${human(r.down)}</span></div>` +
      `<div class="row"><span>Up</span><span class="v">${human(r.up)}</span></div>`));
    hit.addEventListener('mouseleave', hideTip);
    svg.appendChild(hit);
  });

  svg.appendChild(el('line', { x1: m.l, y1: m.t, x2: m.l, y2: H - m.b, class: 'axisline' }));
  host.appendChild(svg);
}

function drawSplit(d) {
  const host = document.getElementById('split');
  host.innerHTML = '';
  const p = d.proxied.down + d.proxied.up, x = d.direct.down + d.direct.up;
  const total = p + x;
  if (!total) { host.innerHTML = '<p class="empty">Nothing recorded in this window.</p>'; return; }

  const W = Math.max(640, host.clientWidth || 640), H = 74, barH = 22, GAP = 2;
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: 'img' });
  const pw = W - 4;
  const wp = Math.max(0, (p / total) * pw - (x > 0 ? GAP : 0));
  const wx = Math.max(0, (x / total) * pw);

  if (p > 0) svg.appendChild(el('path', { d: capPathH(2, 10, wp, barH, 4), fill: cssv('--s1') }));
  if (x > 0) svg.appendChild(el('path', { d: capPathH(2 + wp + GAP, 10, wx, barH, 4), fill: cssv('--dim') }));

  const mk = (x0, color, name, bytes) => {
    const g = el('g', {});
    g.appendChild(el('rect', { x: x0, y: 44, width: 10, height: 10, rx: 3, fill: color }));
    const t1 = el('text', { x: x0 + 16, y: 53, class: 'catlabel' });
    t1.textContent = `${name} \u00b7 ${human(bytes)} (${(bytes / total * 100).toFixed(1)}%)`;
    g.appendChild(t1);
    return g;
  };
  svg.appendChild(mk(2, cssv('--s1'), 'Via proxy', p));
  svg.appendChild(mk(Math.min(pw * 0.5 + 2, W - 260), cssv('--dim'), 'Direct', x));
  host.appendChild(svg);
}

function table(id, cols, rows) {
  const host = document.getElementById(id + '_table');
  host.innerHTML = '<table><thead><tr>' +
    cols.map(c => `<th class="${c.num ? 'num' : ''}">${c.label}</th>`).join('') +
    '</tr></thead><tbody>' +
    rows.map(r => '<tr>' + cols.map(c => `<td class="${c.num ? 'num' : ''}">${c.get(r)}</td>`).join('') + '</tr>').join('') +
    '</tbody></table>';
}

function render() {
  const d = PAYLOAD.data[current];
  const p = d.proxied.down + d.proxied.up, x = d.direct.down + d.direct.up;

  document.getElementById('heroval').textContent = human(p);
  document.getElementById('heronote').textContent =
    `${human(d.proxied.down)} down \u00b7 ${human(d.proxied.up)} up  \u2014  last ${current}`;

  const share = (p + x) ? (p / (p + x) * 100).toFixed(1) + '%' : '\u2014';
  const cov = d.coverage.core ? (d.coverage.seen / d.coverage.core * 100).toFixed(1) + '%' : '\u2014';
  document.getElementById('kpis').innerHTML = `
    <div class="tile"><div class="label">Direct traffic</div><div class="value">${human(x)}</div>
      <div class="note">no plan cost</div></div>
    <div class="tile"><div class="label">Plan share of all traffic</div><div class="value">${share}</div>
      <div class="note">of ${human(p + x)} total</div></div>
    <div class="tile"><div class="label">Attribution coverage</div><div class="value">${cov}</div>
      <div class="note">of what the core counted</div></div>
    <div class="tile"><div class="label">Distinct processes</div><div class="value">${d.processes.length}</div>
      <div class="note">spending plan traffic</div></div>`;

  document.getElementById('timelinecap').textContent =
    `Stacked by process, ${d.bucket >= 3600 ? d.bucket / 3600 + '-hour' : d.bucket / 60 + '-minute'} buckets.`;

  drawTimeline(d);
  drawSplit(d);
  drawRank('processes', d.processes, r => seriesColor(r.key, d.series));
  drawRank('hosts', d.hosts, () => cssv('--s1'));
  drawRank('rules', d.rules, r => r.key === 'Match' ? cssv('--s2') : cssv('--dim'));
  drawRank('outbounds', d.outbounds, r => cssv('--s1'));

  const bytes = k => ({ label: k, num: false });
  table('timeline', [
    { label: 'Time', get: r => clock(r.t, d.bucket) },
    ...d.series.map((n, i) => ({ label: n, num: true, get: r => human(r.v[i]) })),
    { label: 'Total', num: true, get: r => human(r.v.reduce((a, b) => a + b, 0)) },
  ], d.points.filter(r => r.v.some(v => v > 0)));

  const rankCols = name => [
    { label: name, get: r => r.key },
    { label: 'Down', num: true, get: r => human(r.down) },
    { label: 'Up', num: true, get: r => human(r.up) },
    { label: 'Total', num: true, get: r => human(r.down + r.up) },
  ];
  table('processes', rankCols('Process'), d.processes);
  table('hosts', rankCols('Host'), d.hosts);
  table('rules', rankCols('Rule'), d.rules);
  table('outbounds', rankCols('Node'), d.outbounds);
  table('split', [
    { label: 'Path', get: r => r.k },
    { label: 'Down', num: true, get: r => human(r.d) },
    { label: 'Up', num: true, get: r => human(r.u) },
  ], [
    { k: 'Via proxy (plan traffic)', d: d.proxied.down, u: d.proxied.up },
    { k: 'Direct', d: d.direct.down, u: d.direct.up },
  ]);
}

document.getElementById('winbtns').innerHTML = PAYLOAD.windows
  .map(w => `<button class="win" data-w="${w}" aria-pressed="${w === current}">${w}</button>`).join(' ');
document.querySelectorAll('button.win').forEach(b => b.addEventListener('click', () => {
  current = b.dataset.w;
  document.querySelectorAll('button.win').forEach(o => o.setAttribute('aria-pressed', o === b));
  render();
}));
document.querySelectorAll('.tbtn').forEach(b => b.addEventListener('click', () => {
  const id = b.dataset.table;
  const t = document.getElementById(id + '_table'), plot = document.getElementById(id);
  const showing = t.classList.toggle('hidden');
  plot.classList.toggle('hidden', !showing);
  b.textContent = showing ? 'Table' : 'Chart';
}));

const gen = new Date(PAYLOAD.generated * 1000);
document.getElementById('sub').textContent =
  'Generated ' + gen.toLocaleString() + ' \u00b7 rerun dashboard.py to refresh';

render();
addEventListener('resize', () => render());
</script>
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("-o", "--out", default=DEFAULT_OUT)
    parser.add_argument("--open", action="store_true", help="open in the default browser")
    args = parser.parse_args(argv)

    if not os.path.exists(args.db):
        print("no database at %s -- run the collector first" % args.db, file=sys.stderr)
        return 1

    payload = build(args.db)
    html = TEMPLATE.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)

    day = payload["data"].get("24h", {})
    total = day.get("proxied", {}).get("down", 0) + day.get("proxied", {}).get("up", 0)
    print("wrote %s (%.0f KB)" % (args.out, os.path.getsize(args.out) / 1024))
    print("  last 24h plan traffic: %s across %d processes"
          % (human(total), len(day.get("processes", []))))

    if args.open:
        webbrowser.open("file:///" + os.path.abspath(args.out).replace("\\", "/"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
