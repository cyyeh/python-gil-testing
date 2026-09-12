"""Render results/results.json into a self-contained results/report.html (inline SVG
charts, inline CSS/JS, no network).

    python -m bench.report --in results/results.json --out results/report.html
"""

from __future__ import annotations

import argparse
import html
import json
import math
import statistics
from pathlib import Path

CONFIG_ORDER = ["3.12", "3.14 (GIL on)", "3.14t (free-threaded)", "3.14t (PYTHON_GIL=0 forced)"]
FT = "3.14t (free-threaded)"
GIL_ON = "3.14 (GIL on)"
BASE = "3.12"

CATEGORY_TITLES = {
    "cpu": "CPU-bound",
    "io": "I/O-bound",
    "contended": "Contended shared state",
    "multiprocessing": "Multiprocessing reference",
    "library": "Library workloads",
}
CATEGORY_BLURB = {
    "cpu": "Pure-bytecode number crunching split across threads. This is the case the GIL serialises: "
           "expect flat lines on GIL builds and near-linear speed-up on the free-threaded build.",
    "io": "Threads spend their time waiting. The GIL is released while blocked, so all builds overlap the "
          "waits equally - free-threading was never needed here.",
    "contended": "All threads hammer one shared object. Without the GIL, every operation takes the object's "
                 "own lock (or a threading.Lock), so this shows the cost of fine-grained locking.",
    "multiprocessing": "The classic GIL workaround: processes instead of threads. Includes process start-up "
                       "and pickling overhead, and the same work as cpu_primes for direct comparison.",
    "library": "Third-party extension modules. Some release the GIL in C already (so they scale on every build), "
               "some hold it, and some re-enable it for the whole process on import.",
}
STATUS_LABEL = {
    "SUPPORTED": ("good", "Supported"),
    "PURE_PYTHON": ("good", "Pure Python"),
    "REENABLES_GIL": ("critical", "Re-enables GIL"),
    "NOT_INSTALLED": ("serious", "Not installed"),
    "UNKNOWN": ("warning", "Unknown"),
}


# --------------------------------------------------------------------------
# data shaping
# --------------------------------------------------------------------------


def pivot(doc: dict) -> dict:
    out: dict = {}
    for r in doc["records"]:
        if r.get("workers") is None:
            out.setdefault(r["workload"], {}).setdefault(r["config"], {})["error"] = r
            continue
        out.setdefault(r["workload"], {}).setdefault(r["config"], {})[r["workers"]] = r
    return out


NOISY_CV = 0.10  # flag a cell when stdev / mean exceeds this


def cell_stats(rec: dict):
    """Statistics for one cell; falls back to computing from raw seconds for older results."""
    if rec.get("stats"):
        return rec["stats"]
    if rec.get("seconds"):
        from bench.worker import stats
        return stats(rec["seconds"])
    return None


def is_noisy(rec: dict) -> bool:
    st = cell_stats(rec)
    return bool(st and st["cv"] > NOISY_CV)


def _range(by_workers: dict, workers: int):
    rec = by_workers.get(workers)
    st = cell_stats(rec) if rec else None
    return (st["min"], st["max"]) if st else None


def _median(by_workers: dict, workers: int):
    rec = by_workers.get(workers)
    return rec["median"] if rec and rec.get("median") is not None else None


def speedup(by_workers: dict):
    ws = sorted(k for k in by_workers if isinstance(k, int))
    if len(ws) < 2:
        return None
    lo, hi = _median(by_workers, ws[0]), _median(by_workers, ws[-1])
    if not lo or not hi:
        return None
    return lo / hi


def single_thread_ratio(by_config: dict, cfg: str, base: str):
    a, b = _median(by_config.get(cfg, {}), 1), _median(by_config.get(base, {}), 1)
    return a / b if a and b else None


def _configs_present(doc: dict) -> list[str]:
    present = {r["config"] for r in doc["records"]}
    return [c for c in CONFIG_ORDER if c in present] + sorted(present - set(CONFIG_ORDER))


def _fmt_s(v) -> str:
    if v is None:
        return "-"
    return f"{v:.3f} s" if v >= 0.01 else f"{v * 1000:.2f} ms"


def _fmt_x(v) -> str:
    return "-" if v is None else f"{v:.1f}x"


def _pct(ratio) -> str:
    if ratio is None:
        return "-"
    d = (ratio - 1) * 100
    return f"{d:+.0f}%"


# --------------------------------------------------------------------------
# key findings (generated from the data)
# --------------------------------------------------------------------------


def key_findings(doc: dict) -> list[str]:
    p = pivot(doc)
    cats = {w["name"]: w["category"] for w in doc["workloads"]}
    max_w = max(doc["workers"])
    out = []

    for name in [n for n in p if cats.get(n) == "cpu"]:
        parts = [f"{c}: {_fmt_x(speedup(p[name].get(c, {})))}" for c in _configs_present(doc) if c in p[name]]
        out.append(f"CPU-bound {name} at {max_w} threads vs 1 - " + ", ".join(parts) + ".")

    ratios12, ratios14 = [], []
    for name, cat in cats.items():
        if cat in ("cpu", "contended") and name in p:
            r = single_thread_ratio(p[name], FT, BASE)
            if r:
                ratios12.append(r)
            r = single_thread_ratio(p[name], FT, GIL_ON)
            if r:
                ratios14.append(r)
    if ratios12:
        out.append(f"Single-threaded overhead of the free-threaded build (1 worker, median over CPU/contended "
                   f"workloads): {_pct(statistics.median(ratios12))} vs 3.12"
                   + (f", {_pct(statistics.median(ratios14))} vs the same binary with the GIL on." if ratios14 else "."))

    io = [n for n in p if cats.get(n) == "io"]
    if io:
        times = [_median(p[io[0]].get(c, {}), max_w) for c in _configs_present(doc) if c in p[io[0]]]
        times = [t for t in times if t]
        if times:
            spread = (max(times) - min(times)) / min(times) * 100
            out.append(f"I/O-bound {io[0]} is unaffected: all builds overlap waits equally "
                       f"({max_w}-thread times within {spread:.0f}% of each other).")

    for name in [n for n in p if cats.get(n) == "contended"]:
        a, b = _median(p[name].get(FT, {}), max_w), _median(p[name].get(BASE, {}), max_w)
        if a and b:
            out.append(f"Contended {name} at {max_w} threads: free-threaded {_fmt_s(a)} vs 3.12 {_fmt_s(b)} "
                       f"({'slower' if a > b else 'faster'} by {abs(a / b - 1) * 100:.0f}%).")

    if "mp_primes" in p and "cpu_primes" in p:
        mp = _median(p["mp_primes"].get(BASE, {}), max_w)
        th = _median(p["cpu_primes"].get(FT, {}), max_w)
        if mp and th:
            out.append(f"Threads on 3.14t vs processes on 3.12 for the same prime-counting work at {max_w} workers: "
                       f"{_fmt_s(th)} vs {_fmt_s(mp)} - multiprocessing pays start-up and pickling costs.")

    cvs = [cell_stats(r)["cv"] for r in doc["records"] if r.get("median") is not None and cell_stats(r)]
    if cvs:
        n_noisy = sum(1 for cv in cvs if cv > NOISY_CV)
        ns = {cell_stats(r)["n"] for r in doc["records"] if r.get("median") is not None and cell_stats(r)}
        out.append(f"Measurement noise: {min(ns)}{'-' + str(max(ns)) if len(ns) > 1 else ''} timed repeats per cell, "
                   f"median coefficient of variation {statistics.median(cvs) * 100:.1f}%; "
                   f"{n_noisy} of {len(cvs)} cells exceed {NOISY_CV:.0%} (marked ~ in the tables).")

    for pkg, e in doc.get("gil_support", {}).items():
        if e["status"] == "REENABLES_GIL":
            out.append(f"Library '{pkg}' does NOT support free-threading: importing it re-enabled the GIL "
                       f"({e.get('reenabled_by')}), so its '{FT}' results are effectively GIL-on.")
    for name in [n for n in p if cats.get(n) == "library"]:
        parts = [f"{c}: {_fmt_x(speedup(p[name].get(c, {})))}" for c in _configs_present(doc) if c in p[name]]
        out.append(f"Library {name} at {max_w} threads vs 1 - " + ", ".join(parts) + ".")
    return out


# --------------------------------------------------------------------------
# SVG line chart
# --------------------------------------------------------------------------

_MARKERS = ["circle", "square", "diamond", "triangle"]
SHORT = {BASE: "3.12", GIL_ON: "3.14 GIL on", FT: "3.14t no GIL", "3.14t (PYTHON_GIL=0 forced)": "3.14t GIL forced off"}

# Every chart is emitted twice and the page's media queries show exactly one: the wide
# geometry for pointer-sized screens, the narrow one for phones. A single SVG cannot serve
# both - scaling one viewBox down to ~300 px shrinks its text to ~5 px, and CSS can restyle
# text but not re-place it, so the narrow variant is laid out (margins, fonts, markers) for
# the width it is actually displayed at.
WIDE = {"cls": "wide", "W": 680, "H": 320, "ml": 56, "mr": 140, "mt": 36, "mb": 44,
        "r": 4.5, "bar_off": 3.0, "title": True, "y_caption": False, "end_labels": True}
NARROW = {"cls": "narrow", "W": 330, "H": 292, "ml": 44, "mr": 12, "mt": 44, "mb": 44,
          "r": 4.0, "bar_off": 2.5, "title": False, "y_caption": True, "end_labels": False}
Y_LABEL = "median wall time (s) - lower is better"
X_LABEL = "threads / workers"


def _nice_ticks(vmax: float, n: int = 4) -> list[float]:
    if vmax <= 0:
        return [0, 1]
    raw = vmax / n
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if step >= raw:
            break
    ticks = []
    t = 0.0
    while t <= vmax + 1e-12:
        ticks.append(round(t, 10))
        t += step
    ticks.append(round(t, 10))
    return ticks


def _marker(kind: str, x: float, y: float, cls: str, r: float = 4.5) -> str:
    if kind == "circle":
        return f'<circle class="marker {cls}" cx="{x:.1f}" cy="{y:.1f}" r="{r}"/>'
    if kind == "square":
        return f'<rect class="marker {cls}" x="{x - r:.1f}" y="{y - r:.1f}" width="{2 * r}" height="{2 * r}"/>'
    if kind == "diamond":
        return (f'<path class="marker {cls}" d="M{x:.1f} {y - r - 1:.1f} L{x + r + 1:.1f} {y:.1f} '
                f'L{x:.1f} {y + r + 1:.1f} L{x - r - 1:.1f} {y:.1f} Z"/>')
    return (f'<path class="marker {cls}" d="M{x:.1f} {y - r - 1:.1f} L{x + r + 1:.1f} {y + r:.1f} '
            f'L{x - r - 1:.1f} {y + r:.1f} Z"/>')


def line_chart_svg(series: dict, xs: list, title: str, y_label: str = Y_LABEL, x_label: str = X_LABEL,
                   ranges: dict | None = None, geom: dict = WIDE) -> str:
    """One `<svg>` for one geometry (``WIDE`` / ``NARROW``); ``chart_block`` emits both.

    ``ranges`` = {label: {x: (lo, hi)}} draws a min-max error bar behind each marker.
    """
    W, H = geom["W"], geom["H"]
    ml, mr, mt, mb = geom["ml"], geom["mr"], geom["mt"], geom["mb"]
    mkr = geom["r"]  # marker radius
    pw, ph = W - ml - mr, H - mt - mb
    vals = [v for s in series.values() for v in s.values() if v is not None]
    if ranges:
        vals += [hi for r in ranges.values() for rng in r.values() if rng for hi in [rng[1]]]
    vmax = max(vals) if vals else 1.0
    ticks = _nice_ticks(vmax)
    top = ticks[-1]
    xpos = {x: ml + (i + 0.5) * pw / len(xs) for i, x in enumerate(xs)}

    def ypos(v: float) -> float:
        return mt + ph - v / top * ph

    xpos_json = html.escape(json.dumps([round(xpos[x], 1) for x in xs]), quote=True)
    parts = [f'<svg class="chart {geom["cls"]}" viewBox="0 0 {W} {H}" role="img" '
             f'aria-label="{html.escape(title)}" data-xpos="{xpos_json}">']
    if geom["title"]:
        parts.append(f'<text class="chart-title" x="{ml}" y="16">{html.escape(title)}</text>')
    for t in ticks:
        y = ypos(t)
        parts.append(f'<line class="grid" x1="{ml}" x2="{ml + pw}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{ml - 6}" y="{y + 4:.1f}" text-anchor="end">{t:g}</text>')
    for x in xs:
        parts.append(f'<text class="tick" x="{xpos[x]:.1f}" y="{mt + ph + 16}" text-anchor="middle">{x}</text>')
    parts.append(f'<text class="axis-label" x="{ml + pw / 2:.1f}" y="{H - 6}" text-anchor="middle">{html.escape(x_label)}</text>')
    if geom["y_caption"]:
        # no room for a rotated axis on a phone: the unit rides above the plot instead
        parts.append(f'<text class="axis-label" x="2" y="14">{html.escape(y_label)}</text>')
    else:
        parts.append(f'<text class="axis-label" transform="translate(12 {mt + ph / 2:.1f}) rotate(-90)" '
                     f'text-anchor="middle">{html.escape(y_label)}</text>')
    parts.append(f'<line class="crosshair" x1="0" x2="0" y1="{mt}" y2="{mt + ph}" visibility="hidden"/>')

    end_labels = []
    for i, (label, pts) in enumerate(series.items()):
        cls = f"s{i + 1}"
        segs, d = [], ""
        for x in xs:
            v = pts.get(x)
            if v is None:
                segs.append(d)
                d = ""
                continue
            d += ("M" if not d else " L") + f"{xpos[x]:.1f} {ypos(v):.1f}"
        segs.append(d)
        for seg in segs:
            if seg:
                parts.append(f'<path class="series-line {cls}" d="{seg}"/>')
        for x in xs:
            rng = (ranges or {}).get(label, {}).get(x)
            if rng and rng[1] > rng[0]:
                off = geom["bar_off"]
                cx = xpos[x] + (i - (len(series) - 1) / 2) * off  # tiny horizontal offset so bars do not overlap
                lo, hi = ypos(rng[0]), ypos(rng[1])
                parts.append(f'<path class="range {cls}" d="M{cx:.1f} {lo:.1f} L{cx:.1f} {hi:.1f} '
                             f'M{cx - off:.1f} {lo:.1f} L{cx + off:.1f} {lo:.1f} '
                             f'M{cx - off:.1f} {hi:.1f} L{cx + off:.1f} {hi:.1f}"/>')
        last = None
        for x in xs:
            v = pts.get(x)
            if v is not None:
                parts.append(_marker(_MARKERS[i % 4], xpos[x], ypos(v), cls, mkr))
                last = (xpos[x], ypos(v))
        if last:
            end_labels.append((last[1], label, cls, last[0]))

    # direct end labels, but only where they will not collide (otherwise the legend carries identity)
    end_labels.sort()
    if geom["end_labels"]:
        for j, (y, label, cls, x) in enumerate(end_labels):
            near = any(abs(y - other[0]) < 13 for k, other in enumerate(end_labels) if k != j)
            if not near:
                parts.append(f'<text class="end-label" x="{ml + pw + 8}" y="{y + 4:.1f}">{html.escape(SHORT.get(label, label))}</text>')

    parts.append(f'<rect class="hit" x="{ml}" y="{mt}" width="{pw}" height="{ph}" fill="transparent"/>')
    parts.append("</svg>")
    return "".join(parts)


def chart_block(series: dict, xs: list, title: str, y_label: str = Y_LABEL, x_label: str = X_LABEL,
                ranges: dict | None = None) -> str:
    """A chart that fits any viewport: both geometries, one legend, one tooltip."""
    svgs = "".join(line_chart_svg(series, xs, title, y_label, x_label, ranges, g) for g in (WIDE, NARROW))
    # legend (always present for >= 2 series) lives in HTML below the plot so it can never collide
    # with labels, and so it reflows instead of overflowing on a narrow screen
    legend = "".join(
        f'<span class="legend-item"><svg class="legend-glyph" viewBox="0 0 14 14" aria-hidden="true">'
        f'{_marker(_MARKERS[i % 4], 7, 7, f"s{i + 1}")}</svg>{html.escape(label)}</span>'
        for i, label in enumerate(series))
    data = {"xs": xs,
            "series": [{"label": l, "short": SHORT.get(l, l), "vals": [pts.get(x) for x in xs],
                        "rng": [(ranges or {}).get(l, {}).get(x) for x in xs]} for l, pts in series.items()]}
    return (f'<div class="chart-wrap" data-chart=\'{html.escape(json.dumps(data), quote=True)}\'>'
            + svgs + f'<div class="legend-row">{legend}</div><div class="tooltip" hidden></div></div>')


# --------------------------------------------------------------------------
# HTML pieces
# --------------------------------------------------------------------------


def tip(inner: str, explanation: str, cls: str = "abbr") -> str:
    """Wrap a glyph/badge/abbreviation so it explains itself on hover, focus, or tap.

    Rule for every HTML this repo generates: a symbol never stands alone - it carries a
    ``data-tip`` (immediate, styled, touch-friendly; native ``title`` is delayed and
    invisible on touch) and is keyboard-focusable.
    """
    return f'<span class="{cls} has-tip" tabindex="0" data-tip="{html.escape(explanation, quote=True)}">{inner}</span>'


GIL_FLAG_TIP = ("GIL re-enabled: an extension module imported by this workload has not declared free-threading "
                "support, so CPython turned the GIL back on for this run. This 'free-threaded' number is "
                "effectively GIL-on; see the support table.")
SPEEDUP_TIP = "1-thread median divided by the highest-thread-count median. Above 1 = faster with more threads; 1 = no scaling."
GIL_AT_RUN_TIP = ("sys._is_gil_enabled() measured inside the benchmark process. It can differ from the build "
                  "default: PYTHON_GIL=1 turns it on, and an extension without free-threading support turns it "
                  "back on at import.")
STATUS_TIPS = {
    "SUPPORTED": "Imported on the free-threaded 3.14t interpreter; compiled extension modules were loaded and the GIL stayed off.",
    "PURE_PYTHON": "No compiled extension modules: runs without the GIL automatically. Thread-safety of its own data structures is still the library's job.",
    "REENABLES_GIL": "One of its extension modules lacks Py_MOD_GIL_NOT_USED, so CPython re-enabled the GIL for the whole process at import. Everything in that process, including other libraries, loses free-threading.",
    "NOT_INSTALLED": "The import failed in the 3.14t environment (often: no cp314t wheel available).",
    "UNKNOWN": "Could not determine: not probed on a free-threaded interpreter, or the GIL was already forced on.",
}


def _gil_flag() -> str:
    return tip("&#9888;", GIL_FLAG_TIP, "flag")


# On a phone a table is only as readable as its widest column, so configuration labels get a
# short form there. It is an abbreviation, so - like every symbol on the page - it explains
# itself on hover / focus / tap.
ABBREV = {BASE: "3.12", GIL_ON: "3.14 GIL", FT: "3.14t", "3.14t (PYTHON_GIL=0 forced)": "3.14t GIL=0"}


def _config_label(c: str) -> str:
    """The full configuration label, with a shorter one swapped in by CSS on narrow screens."""
    short = ABBREV.get(c)
    if not short or short == c:
        return html.escape(c)
    return (f'<span class="only-wide">{html.escape(c)}</span>'
            f'<span class="only-narrow">{tip(html.escape(short), f"{c} (shortened to fit this screen)")}</span>')


def _scroll(table: str) -> str:
    """Wrap a table so it scrolls sideways inside the page instead of widening it."""
    return f'<div class="table-scroll" tabindex="0">{table}</div>'


def _badge(kind: str, text: str, status: str = "") -> str:
    icon = {"good": "&#10003;", "warning": "?", "serious": "!", "critical": "&#10007;"}[kind]
    inner = f'<span class="badge-icon">{icon}</span>{html.escape(text)}'
    return tip(inner, STATUS_TIPS.get(status, text), f"badge {kind}")


def workload_table(by_config: dict, workers: list[int], configs: list[str]) -> str:
    head = "".join(f"<th>{w} thr</th>" for w in workers)
    rows = []
    for c in configs:
        bw = by_config.get(c)
        if not bw:
            continue
        cells = []
        for w in workers:
            rec = bw.get(w)
            if rec is None:
                cells.append("<td>-</td>")
            elif rec.get("skipped"):
                cells.append("<td>" + tip("skipped", "Not measured: " + str(rec["skipped"])) + "</td>")
            elif rec.get("error"):
                cells.append('<td class="err">'
                             + tip("error", "This cell raised; the traceback is in the details block below the table.")
                             + "</td>")
            else:
                flag = f" {_gil_flag()}" if c == FT and rec.get("gil_enabled") else ""
                st = cell_stats(rec)
                pm = ""
                if st:
                    noisy = ""
                    if st["cv"] > NOISY_CV:
                        noisy = " " + tip("~", f"Noisy cell: coefficient of variation {st['cv']:.0%} (stdev / mean) is above "
                                               f"{NOISY_CV:.0%}. Differences smaller than this spread are not meaningful; "
                                               f"re-run with more repeats (--repeats 10) to confirm.", "noisy")
                    pm = tip(f"&plusmn;{_fmt_s(st['stdev'])}",
                             f"{st['n']} timed repeats: median {_fmt_s(st['median'])}, mean {_fmt_s(st['mean'])}, "
                             f"sample stdev {_fmt_s(st['stdev'])}, min {_fmt_s(st['min'])}, max {_fmt_s(st['max'])}, "
                             f"cv {st['cv']:.1%}.", "pm") + noisy
                cells.append(f"<td>{_fmt_s(rec['median'])} {pm}{flag}</td>")
        sp = speedup(bw)
        rows.append(f"<tr><th scope=\"row\"><span class=\"swatch s{configs.index(c) + 1}\"></span>{_config_label(c)}</th>"
                    + "".join(cells) + f"<td class=\"num\">{_fmt_x(sp)}</td></tr>")
    return (_scroll(f'<table class="data"><thead><tr><th>configuration</th>{head}'
                    f'<th>{tip(f"speed-up {workers[0]}&rarr;{workers[-1]}", SPEEDUP_TIP)}</th>'
                    f"</tr></thead><tbody>{''.join(rows)}</tbody></table>")
            + '<p class="muted small">median &plusmn; sample standard deviation over the timed repeats; '
              'hover, focus or tap &plusmn; for min / max; ~ marks a noisy cell (cv &gt; 10%).</p>')


def _workload_block(w: dict, by_config: dict, doc: dict, configs: list[str]) -> str:
    workers = doc["workers"]
    series = {c: {k: _median(by_config[c], k) for k in workers} for c in configs if c in by_config}
    ranges = {c: {k: _range(by_config[c], k) for k in workers} for c in series}
    flagged = any(c == FT and by_config[c].get(k, {}).get("gil_enabled") for c in series for k in workers)
    note = ""
    if flagged:
        note = ('<p class="callout critical"><strong>GIL re-enabled.</strong> On the free-threaded build an extension '
                'module imported by this workload re-enabled the GIL, so the "3.14t (free-threaded)" line ran '
                'with the GIL on. See the support table above.</p>')
    errors = "".join(f'<details><summary>error on {html.escape(c)}</summary><pre>{html.escape(str(bc["error"].get("error") or bc["error"]))}</pre></details>'
                     for c, bc in by_config.items() if "error" in bc)
    err_recs = "".join(f'<details><summary>error on {html.escape(c)} ({k} thr)</summary><pre>{html.escape(r["error"][-1500:])}</pre></details>'
                       for c, bc in by_config.items() for k, r in bc.items() if isinstance(k, int) and r.get("error"))
    return (f'<article class="workload" id="w-{w["name"]}"><h4><code>{w["name"]}</code></h4>'
            f'<p class="desc">{html.escape(w["description"])}</p>{note}'
            + chart_block(series, workers, w["name"], ranges=ranges) + workload_table(by_config, workers, configs)
            + errors + err_recs + "</article>")


def _support_table(gil_support: dict) -> str:
    if not gil_support:
        return ""
    rows = []
    for pkg, e in gil_support.items():
        kind, label = STATUS_LABEL.get(e["status"], ("warning", e["status"]))
        rows.append(f"<tr><th scope=\"row\"><code>{html.escape(pkg)}</code></th><td>{html.escape(str(e.get('version') or '-'))}</td>"
                    f"<td><code>{html.escape(str(e.get('wheel_abi') or '-'))}</code></td>"
                    f"<td>{_badge(kind, label, e['status'])} <code class=\"muted\">{html.escape(e['status'])}</code></td>"
                    f"<td class=\"detail\">{html.escape(e['detail'])}</td></tr>")
    return ('<h4>Free-threading support check (<code>check_gil_support.py</code>, probed on 3.14t)</h4>'
            + _scroll('<table class="data support"><thead><tr><th>package</th><th>version</th><th>wheel ABI</th>'
                      f'<th>status</th><th>detail</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'))


def _summary_table(doc: dict, p: dict, configs: list[str]) -> str:
    max_w = max(doc["workers"])
    head = "".join(f"<th>{_config_label(c)}</th>" for c in configs)
    rows = []
    for w in doc["workloads"]:
        if w["name"] not in p:
            continue
        cells = []
        for c in configs:
            bw = p[w["name"]].get(c)
            if not bw:
                cells.append("<td>-</td>")
                continue
            sp, t = speedup(bw), _median(bw, max_w)
            flag = f" {_gil_flag()}" if c == FT and bw.get(max_w, {}).get("gil_enabled") else ""
            cells.append(f'<td><strong>{_fmt_x(sp)}</strong> <span class="muted">{_fmt_s(t)}</span>{flag}</td>')
        # the category is dropped on narrow screens: it would push the pinned first column
        # across the whole viewport, and the workloads are grouped by category below anyway
        rows.append(f'<tr><th scope="row"><a href="#w-{w["name"]}"><code>{w["name"]}</code></a> '
                    f'<span class="muted only-wide">{CATEGORY_TITLES.get(w["category"], w["category"])}</span></th>'
                    f'{"".join(cells)}</tr>')
    return (_scroll(f'<table class="data summary"><thead><tr><th>workload</th>{head}</tr></thead>'
                    f'<tbody>{"".join(rows)}</tbody></table>')
            + f'<p class="muted small">Cells: {tip("speed-up", SPEEDUP_TIP)} from 1 to {max_w} workers, and wall time at {max_w} workers. '
              f'{_gil_flag()} = the free-threaded run actually had the GIL re-enabled by an extension module '
              '(hover, focus or tap any symbol for its meaning).</p>')


def _config_table(doc: dict) -> str:
    rows = []
    for c in doc["configs"]:
        info, f = c.get("info") or {}, c.get("facts") or {}
        rows.append(f"<tr><th scope=\"row\">{_config_label(c['label'])}</th><td>{html.escape(str(info.get('version', '?')))}</td>"
                    f"<td>{'yes' if info.get('free_threaded_build') else 'no'}</td>"
                    f"<td>{'yes' if info.get('gil_enabled', True) else 'no'}</td>"
                    f"<td><code>{html.escape(' '.join(f'{k}={v}' for k, v in c.get('env', {}).items() if k == 'PYTHON_GIL') or '-')}</code></td>"
                    f"<td class=\"detail\">{html.escape(c.get('note', ''))}</td></tr>")
    return _scroll('<table class="data"><thead><tr><th>label</th><th>version</th>'
                   f'<th>{tip("free-threaded build", "sysconfig.get_config_var(&quot;Py_GIL_DISABLED&quot;): was this binary built with --disable-gil (python3.14t)?")}</th>'
                   f'<th>{tip("GIL enabled at run", GIL_AT_RUN_TIP)}</th><th>env</th><th>note</th></tr></thead><tbody>'
                   + "".join(rows) + "</tbody></table>")


def _facts_table(doc: dict) -> str:
    cfgs = [c for c in doc["configs"] if c.get("facts")]
    if not cfgs:
        return ""
    head = "".join(f"<th>{_config_label(c['label'])}</th>" for c in cfgs)
    rows = []
    scalar = [("Python version", "version"), ("ABI flags (sys.abiflags)", "abiflags"), ("SOABI", "soabi"),
              ("Free-threaded build (Py_GIL_DISABLED)", "free_threaded_build"), ("GIL enabled at run", "gil_enabled"),
              ("sys.flags.gil (None = build default)", "gil_flag"), ("sys.flags.thread_inherit_context", "thread_inherit_context"),
              ("sys.flags.context_aware_warnings", "context_aware_warnings"), ("JIT available (sys._jit)", "jit_available"),
              ("gc.get_threshold()", "gc_threshold"), ("compiler", "compiler")]
    for label, key in scalar:
        cells = "".join(f"<td><code>{html.escape(str(c['facts'].get(key)))}</code></td>" for c in cfgs)
        rows.append(f"<tr><th scope=\"row\">{html.escape(label)}</th>{cells}</tr>")
    sizes = list(cfgs[0]["facts"].get("sizeof", {}))
    for name in sizes:
        cells = "".join(f"<td>{c['facts']['sizeof'].get(name, '-')}</td>" for c in cfgs)
        rows.append(f"<tr><th scope=\"row\">sys.getsizeof(<code>{html.escape(name)}</code>) bytes</th>{cells}</tr>")
    return _scroll(f'<table class="data facts"><thead><tr><th>measured fact</th>{head}</tr></thead>'
                   f'<tbody>{"".join(rows)}</tbody></table>')


def _impl_differences(doc: dict) -> str:
    return f"""
<h2 id="impl">Implementation differences: what actually changed inside CPython</h2>
<p>All statements below about <em>this machine</em> are measured (see the facts table); the rest describes CPython's
design as documented in PEP&nbsp;703 (free-threaded CPython), PEP&nbsp;779 (free-threading officially supported in 3.14),
PEP&nbsp;683 (immortal objects) and the CPython free-threading HOWTOs.</p>

<h3>1. Timeline</h3>
<ul>
<li><strong>3.12</strong> - one GIL per interpreter (PEP&nbsp;684 made it per-<em>sub</em>interpreter for the C-API). Threads never
run bytecode concurrently.</li>
<li><strong>3.13</strong> - PEP&nbsp;703 lands as an <em>experimental</em> separate build (<code>--disable-gil</code>, binary
<code>python3.13t</code>). Specialisation was switched off in that build, costing ~40% single-thread performance.</li>
<li><strong>3.14</strong> - PEP&nbsp;779: the free-threaded build is <em>officially supported</em> (no longer experimental). The
specialising interpreter is back on, single-thread overhead is quoted by the core team at roughly 5-10%, and the build is what
this report calls <code>3.14t</code>. The default (non-t) 3.14 build is unchanged and still has the GIL.</li>
</ul>

<h3>2. Reference counting: biased, deferred, immortal</h3>
<p>With the GIL, <code>Py_INCREF</code> is a plain non-atomic increment. Without it, every increment from every thread would
have to be atomic - a large slowdown. Free-threaded CPython uses three tricks:</p>
<ul>
<li><strong>Biased reference counting</strong> - each object records its owning thread (<code>ob_tid</code>) and keeps
<em>two</em> counters: <code>ob_ref_local</code>, touched only by the owner without atomics, and <code>ob_ref_shared</code>,
an atomic counter used by every other thread. The true count is their sum; they are merged when the object is freed or its
ownership changes.</li>
<li><strong>Immortal objects</strong> (PEP&nbsp;683) - <code>None</code>, <code>True</code>, small ints, interned strings, static
types etc. have a saturated count and are never inc/dec'd, so hot shared constants create no cache-line traffic.</li>
<li><strong>Deferred reference counting</strong> - functions, code objects, modules and top-level classes are referenced from
the interpreter's evaluation stack without counting; the garbage collector accounts for those references instead. 3.14 goes
further with tagged <em>stack references</em> in the evaluation loop.</li>
</ul>
<p>The price is a bigger object header: <code>ob_tid</code>, a one-byte <code>ob_mutex</code>, GC bits and the two counters.
<strong>Measured here:</strong> <code>sys.getsizeof(object())</code> is 16 bytes on 3.12 and 32 on 3.14t; a small
<code>int</code> and a short <code>str</code> each grow by 16 bytes.</p>

<h3>3. Memory allocator and garbage collector</h3>
<ul>
<li><strong>mimalloc</strong> replaces <code>pymalloc</code>. It has per-thread heaps, so allocation needs no global lock, and
its heap structures let the collector <em>enumerate</em> objects.</li>
<li>Because of that, the free-threaded build drops the 16-byte <code>PyGC_Head</code> doubly-linked-list pre-header that
every GC-tracked object carries on 3.12. <strong>Measured here:</strong> <code>[]</code>, <code>{{}}</code> and class
instances are the <em>same</em> size on both builds - the bigger object header is exactly offset by the missing GC header.</li>
<li>The cycle collector is <strong>stop-the-world</strong>: it pauses every thread at a safe point (the "eval breaker"
check), runs a single-generation collection, then resumes them. The default (GIL) 3.14 build keeps its generational
collector. Note that <code>gc.get_threshold()</code> reads <code>(2000, 10, 10)</code> on both 3.14 builds versus
<code>(700, 10, 10)</code> on 3.12 - that is a 3.12&rarr;3.14 change, not a free-threading one.</li>
</ul>

<h3>4. Per-object locks and critical sections</h3>
<p>The GIL used to make <code>list.append</code>, <code>dict.__setitem__</code> and friends safe by accident. The free-threaded
build makes them safe on purpose: each object has a one-byte lock (<code>ob_mutex</code>) and C code wraps mutating
operations in <em>critical sections</em> (<code>Py_BEGIN_CRITICAL_SECTION</code>). Critical sections are automatically
suspended when a thread blocks (e.g. on another lock), which avoids lock-ordering deadlocks. Most <em>reads</em> of lists and
dicts are lock-free, protected by quiescent-state-based reclamation (QSBR) so memory is never freed under a reader.</p>
<p>What this guarantees is <em>memory safety</em> of the interpreter, not atomicity of your program: <code>d[k] += 1</code>
is still a read-modify-write race, exactly as it was with the GIL - only the interleavings become more frequent. That is why
<code>contended_dict_update</code> above uses an explicit <code>threading.Lock</code>, and why
<code>contended_list_append</code> shows the cost of taking the list's lock on every call.</p>

<h3>5. The interpreter loop</h3>
<ul>
<li>The specialising adaptive interpreter rewrites bytecodes in place, which is a data race if two threads run the same code
object. 3.14t solves this with <strong>thread-local bytecode</strong> (<code>-X tlbc</code>, <code>PYTHON_TLBC</code>): each
thread specialises its own copy. This is what brought single-thread overhead down from ~40% (3.13t) to the level measured in
the "single-threaded overhead" finding above.</li>
<li>The experimental JIT is not available in the free-threaded build (<code>sys._jit.is_available()</code> is
<code>False</code> here).</li>
<li>Thread switching no longer exists as a concept: there is no <code>sys.setswitchinterval</code> hand-off; threads simply run.</li>
</ul>

<h3>6. Runtime knobs and semantics visible from Python</h3>
<ul>
<li><code>sysconfig.get_config_var("Py_GIL_DISABLED")</code> - is this a free-threaded <em>build</em>?</li>
<li><code>sys._is_gil_enabled()</code> - is the GIL on <em>right now</em>? (It can be re-enabled at runtime, see &sect;7.)</li>
<li><code>PYTHON_GIL=0|1</code> / <code>-X gil=0|1</code> and <code>sys.flags.gil</code> - override the build default. This
report's "3.14 (GIL on)" column is the free-threaded binary run with <code>PYTHON_GIL=1</code>: same allocator, same headers,
same locks - only the GIL is re-added. It isolates the GIL's effect but is <em>not</em> identical to a stock 3.14 build.</li>
<li>On the free-threaded build new threads <strong>inherit the caller's context variables</strong> by default
(<code>sys.flags.thread_inherit_context = 1</code>) and <code>warnings.catch_warnings</code> becomes context-aware
(<code>sys.flags.context_aware_warnings = 1</code>), because module-global mutable state is no longer protected by the GIL.</li>
<li><code>concurrent.futures.ThreadPoolExecutor</code> and plain <code>threading.Thread</code> are unchanged; they simply
scale now. <code>asyncio</code> is still single-threaded per loop, and <code>multiprocessing</code> still works.</li>
</ul>

<h3>7. C extensions and packaging</h3>
<ul>
<li>Extension modules must opt in by declaring <code>Py_mod_gil = Py_MOD_GIL_NOT_USED</code> (multi-phase init) or calling
<code>PyUnstable_Module_SetGIL(m, Py_MOD_GIL_NOT_USED)</code>. If a module does not, CPython <strong>re-enables the GIL for
the whole process</strong> and emits a <code>RuntimeWarning</code>. That is precisely what <code>check_gil_support.py</code>
detects, and what happened with <code>duckdb</code> in this run.</li>
<li>Wheels are tagged with the <code>t</code> ABI (<code>cp314-cp314t-...</code>, <code>SOABI=cpython-314t-darwin</code>);
they are not interchangeable with <code>cp314</code> wheels. The stable ABI (<code>abi3</code>) does not cover free-threaded
builds in 3.13/3.14, so every extension needs a dedicated build.</li>
<li>A pure-Python wheel (<code>py3-none-any</code>) runs on the free-threaded build automatically, but "runs" is not
"thread-safe": the library still has to protect its own global state.</li>
<li>Extensions that already released the GIL around long C loops (numpy's BLAS calls, DuckDB's query engine, scikit-learn's
Cython <code>nogil</code> blocks) scaled across threads <em>before</em> free-threading; the gain from 3.14t is for code that
holds the GIL - Python-level dispatch, small-array ops, pandas' Cython paths, and ordinary Python code.</li>
</ul>

<h3>8. Measured facts (both builds on this machine)</h3>
{_facts_table(doc)}
"""


def _methodology(doc: dict) -> str:
    return f"""
<h2 id="method">Methodology &amp; how to reproduce</h2>
<ul>
<li>Each (configuration, workload) pair runs in a <strong>fresh subprocess</strong> so an import that re-enables the GIL cannot
leak into other measurements. Each thread count is timed {doc['repeats']}x (<code>--repeats</code>, default 5); the
<strong>median</strong> is plotted, error bars show the min-max range, and tables give median &plusmn; sample standard
deviation. Cells whose coefficient of variation exceeds {NOISY_CV:.0%} are marked ~; treat differences smaller than the
error bars as noise.</li>
<li>Every workload returns a value that must be identical for every thread count (checked by <code>tests/</code>), so each run
does the same total work.</li>
<li>BLAS/OpenMP are pinned to one thread (<code>OPENBLAS_NUM_THREADS=1</code> etc.) so scaling reflects Python threads only.
Every workload gets one untimed warm-up call first (cold caches, lazy imports, uvicorn start-up).</li>
<li>Thread counts: {', '.join(map(str, doc['workers']))} on a {html.escape(str(doc['host'].get('ncpu')))}-core
{html.escape(str(doc['host'].get('cpu')))} ({html.escape(str(doc['host'].get('os')))}).{' <strong>Quick mode</strong> (reduced problem sizes).' if doc.get('quick') else ''}</li>
</ul>
<pre><code>./setup.sh                                  # uv installs 3.12 + 3.14t, creates .venv312 / .venv314t
.venv312/bin/python -m bench.run            # bare-Python workloads (default)
.venv312/bin/python -m bench.run --libs     # + numpy / pandas / duckdb / scikit-learn / fastapi
.venv312/bin/python -m bench.compare_lib numpy   # one library: support check -&gt; warn -&gt; bench -&gt; report
.venv312/bin/python -m bench.report         # regenerate results/report.html
.venv314t/bin/python check_gil_support.py numpy duckdb   # does a package support free-threading?</code></pre>
"""


_CSS = """
:root {
  color-scheme: light;
  --bg: #fcfcfb; --surface: #ffffff; --line: #e6e5e1; --grid: #ecebe7;
  --text: #0b0b0b; --text-2: #52514e; --text-3: #7d7b76;
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --s4: #eda100;
  --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;
  --callout-bg: #fbeaea; --code-bg: #f3f2ee;
  --cell-bg: var(--bg);  /* opaque backdrop for the sticky first column of a scrolling table */
  --edge-shadow: rgba(0,0,0,.20);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --bg: #1a1a19; --surface: #222221; --line: #3a3a37; --grid: #2e2e2c;
    --text: #ffffff; --text-2: #c3c2b7; --text-3: #8f8e88;
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
    --callout-bg: #3a2222; --code-bg: #2a2a28;
    --edge-shadow: rgba(255,255,255,.22);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --bg: #1a1a19; --surface: #222221; --line: #3a3a37; --grid: #2e2e2c;
  --text: #ffffff; --text-2: #c3c2b7; --text-3: #8f8e88;
  --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500;
  --callout-bg: #3a2222; --code-bg: #2a2a28;
  --edge-shadow: rgba(255,255,255,.22);
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       overflow-wrap: break-word; }
main { max-width: 1040px; margin: 0 auto; padding: 24px 20px 80px; }
h1 { font-size: 30px; line-height: 1.2; margin: 0 0 6px; letter-spacing: -0.01em; }
h2 { font-size: 22px; margin: 48px 0 12px; padding-top: 12px; border-top: 1px solid var(--line); }
h3 { font-size: 17px; margin: 28px 0 8px; }
h4 { font-size: 15px; margin: 18px 0 4px; }
p { margin: 8px 0; }
code { font: 13px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background: var(--code-bg); padding: 1px 4px; border-radius: 4px; }
pre { background: var(--code-bg); padding: 12px 14px; border-radius: 8px; overflow-x: auto; font-size: 13px; }
pre code { background: none; padding: 0; }
.muted { color: var(--text-2); }
.small { font-size: 13px; }
.sub { color: var(--text-2); margin: 0 0 18px; }
nav.toc { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 14px; margin: 10px 0 20px; }
nav.toc a { color: var(--text-2); text-decoration: none; border-bottom: 1px solid var(--line); padding: 3px 0; }
nav.toc a:hover { color: var(--text); }
.findings { padding-left: 20px; }
.findings li { margin: 6px 0; }
table.data { border-collapse: collapse; width: 100%; font-size: 13.5px; margin: 8px 0 18px; }
table.data th, table.data td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
table.data thead th { color: var(--text-2); font-weight: 600; border-bottom: 1px solid var(--text-3); }
table.data tbody th { font-weight: 500; }
/* cells stay on one line: a table that cannot fit scrolls (see .table-scroll) instead of
   shredding its columns into three-line stacks */
table.data th, table.data td { white-space: nowrap; }
table.data td.num { font-variant-numeric: tabular-nums; }
table.data td.detail, table.data td .detail { color: var(--text-2); }
table.data td.detail { white-space: normal; min-width: 14em; }
table.data td.err { color: var(--critical); }
table.support td:nth-child(4) { white-space: nowrap; }
/* Tables keep their cells intact and scroll sideways on their own rather than widening the page.
   The two radial layers are edge shadows that appear only on the side there is more table to
   reach, and the two flat layers above them hide those shadows once that end is reached. */
.table-scroll { overflow-x: auto; overscroll-behavior-x: contain; -webkit-overflow-scrolling: touch; margin: 8px 0 18px;
  background:
    linear-gradient(to right, var(--cell-bg) 60%, transparent) left center / 22px 100% no-repeat local,
    linear-gradient(to left, var(--cell-bg) 60%, transparent) right center / 22px 100% no-repeat local,
    radial-gradient(farthest-side at 0 50%, var(--edge-shadow), transparent) left center / 11px 100% no-repeat scroll,
    radial-gradient(farthest-side at 100% 50%, var(--edge-shadow), transparent) right center / 11px 100% no-repeat scroll; }
.table-scroll > table.data { margin: 0; width: auto; min-width: 100%; }
.table-scroll table.data tbody th, .table-scroll table.data thead th:first-child {
  position: sticky; left: 0; z-index: 1; background: var(--cell-bg); }
.table-scroll:focus-visible { outline: 2px solid var(--s1); outline-offset: 2px; }
.only-narrow { display: none; }
.swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; vertical-align: 1px; }
.swatch.s1 { background: var(--s1); } .swatch.s2 { background: var(--s2); } .swatch.s3 { background: var(--s3); } .swatch.s4 { background: var(--s4); }
.flag { color: var(--critical); }
.pm { color: var(--text-3); font-size: 12px; font-variant-numeric: tabular-nums; }
.noisy { color: var(--warning); font-weight: 700; }
.has-tip { cursor: help; border-bottom: 1px dotted var(--text-3); outline-offset: 2px; }
.has-tip.badge, .has-tip.flag, .has-tip.noisy { border-bottom: none; }
.tip { position: fixed; z-index: 10; width: max-content; max-width: min(340px, calc(100vw - 20px)); background: var(--surface); color: var(--text); border: 1px solid var(--line);
       border-radius: 6px; padding: 8px 10px; font-size: 12.5px; line-height: 1.45; box-shadow: 0 4px 14px rgba(0,0,0,.18); pointer-events: none; }
.badge { display: inline-flex; align-items: center; gap: 5px; font-size: 12.5px; font-weight: 600; padding: 1px 8px 1px 6px; border-radius: 999px; border: 1px solid currentColor; }
.badge.good { color: var(--good); } .badge.warning { color: var(--warning); } .badge.serious { color: var(--serious); } .badge.critical { color: var(--critical); }
.badge-icon { font-weight: 700; }
.callout { padding: 10px 14px; border-radius: 8px; border-left: 4px solid var(--critical); background: var(--callout-bg); }
.category { margin-top: 28px; }
.category > p.blurb { color: var(--text-2); }
article.workload { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px 6px; margin: 14px 0;
                   --cell-bg: var(--surface); }
article.workload h4 { margin: 0 0 2px; }
article.workload p.desc { margin: 0 0 6px; color: var(--text-2); font-size: 14px; }
.chart-wrap { position: relative; margin: 6px 0 4px; }
svg.chart { width: 100%; height: auto; display: block; font-family: inherit; }
svg.chart.wide { max-width: 680px; }
svg.chart.narrow { display: none; max-width: 380px; }
svg.chart.narrow .tick { font-size: 13px; }
svg.chart.narrow .axis-label { font-size: 12px; }
svg.chart.narrow .series-line { stroke-width: 2.4; }
svg.chart.narrow .range { stroke-width: 1.8; }
svg.chart.narrow .marker { stroke-width: 1.6; }
svg.chart .chart-title { font-size: 13px; font-weight: 600; fill: var(--text); }
svg.chart .tick { font-size: 11px; fill: var(--text-2); }
svg.chart .axis-label { font-size: 11px; fill: var(--text-3); }
svg.chart .grid { stroke: var(--grid); stroke-width: 1; }
svg.chart .series-line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
svg.chart .marker { stroke: var(--surface); stroke-width: 2; }
svg.chart .range { fill: none; stroke-width: 1.5; opacity: .55; }
svg.chart .s1 { stroke: var(--s1); } svg.chart .s2 { stroke: var(--s2); } svg.chart .s3 { stroke: var(--s3); } svg.chart .s4 { stroke: var(--s4); }
svg.chart .marker.s1 { fill: var(--s1); } svg.chart .marker.s2 { fill: var(--s2); } svg.chart .marker.s3 { fill: var(--s3); } svg.chart .marker.s4 { fill: var(--s4); }
svg.chart .end-label { font-size: 11px; fill: var(--text-2); }
.legend-row { display: flex; flex-wrap: wrap; gap: 4px 18px; font-size: 12.5px; color: var(--text-2); margin: 2px 0 8px 56px; }
.legend-item { display: inline-flex; align-items: center; gap: 6px; }
.legend-glyph { width: 14px; height: 14px; }
.legend-glyph .marker { stroke: var(--surface); stroke-width: 1.5; }
.legend-glyph .marker.s1 { fill: var(--s1); } .legend-glyph .marker.s2 { fill: var(--s2); } .legend-glyph .marker.s3 { fill: var(--s3); } .legend-glyph .marker.s4 { fill: var(--s4); }
svg.chart .crosshair { stroke: var(--text-3); stroke-width: 1; }
/* pan-y: a vertical swipe still scrolls the page, a horizontal drag reads off the chart */
svg.chart .hit { cursor: crosshair; touch-action: pan-y; }
.tooltip { position: absolute; pointer-events: none; background: var(--surface); color: var(--text); border: 1px solid var(--line); border-radius: 6px; padding: 6px 10px; font-size: 12px; box-shadow: 0 2px 8px rgba(0,0,0,.12); min-width: 150px; max-width: 100%; }
.tooltip .t-x { color: var(--text-2); margin-bottom: 3px; }
.tooltip .t-row { display: flex; justify-content: space-between; gap: 10px; }
.tooltip .t-row strong { font-variant-numeric: tabular-nums; }
.tooltip .t-r { font-weight: 400; color: var(--text-3); font-size: 11px; }
.tooltip .t-row .t-l { display: inline-flex; align-items: center; gap: 5px; color: var(--text-2); }
.tooltip .t-l i { display: inline-block; width: 8px; height: 8px; border-radius: 50%; }
details { margin: 6px 0; } summary { cursor: pointer; color: var(--text-2); font-size: 13px; padding: 2px 0; }

/* ---------------------------------------------------------------- phones and small tablets
   620px is where the wide chart's 11px type would start rendering below ~9px: from here on
   the narrow chart variant takes over, headings and padding shrink, and the tables fall back
   to sideways scrolling with the configuration column pinned and abbreviated. */
@media (max-width: 620px) {
  body { font-size: 14px; }
  main { padding: 18px 14px 56px; }
  h1 { font-size: 23px; }
  h2 { font-size: 19px; margin: 36px 0 10px; }
  h3 { font-size: 16px; margin: 22px 0 6px; }
  nav.toc { gap: 4px 14px; font-size: 13.5px; }
  main ul { padding-left: 22px; }  /* the browser default eats a tenth of a phone screen */
  .findings { padding-left: 18px; }
  pre { font-size: 12px; padding: 10px 12px; }
  article.workload { padding: 12px 12px 4px; }
  article.workload p.desc { font-size: 13.5px; }
  table.data { font-size: 12.5px; }
  table.data th, table.data td { padding: 5px 7px; }
  table.data td.detail { min-width: 20em; }  /* wide enough that a long note stays a few lines tall */
  table.summary td > span.muted { display: block; }  /* wall time under the speed-up, not beside it */
  /* the pinned column overlaps the cells sliding under it, so it needs to read as a layer */
  .table-scroll table.data tbody th, .table-scroll table.data thead th:first-child {
    box-shadow: 1px 0 0 var(--line), 5px 0 6px -5px var(--edge-shadow); }
  .only-wide { display: none; }
  .only-narrow { display: inline; }
  .legend-row { gap: 2px 14px; }
  .tooltip { min-width: 0; font-size: 11.5px; padding: 5px 8px; }
}
/* The chart swaps earlier than the rest of the layout: below ~700px the wide geometry would
   be scaled to under 620px, which takes its 11px type below 10px. */
@media (max-width: 700px) {
  svg.chart.wide { display: none; }
  svg.chart.narrow { display: block; }
  .legend-row { margin-left: 0; }
}
@media (max-width: 400px) {
  main { padding: 16px 10px 48px; }
  h1 { font-size: 21px; }
  article.workload { padding: 10px 10px 4px; border-radius: 8px; }
  table.data { font-size: 12px; }
  .legend-row { font-size: 11.5px; }
}
"""

_JS = """
(function () {
  // Explanation tooltips: every element with data-tip shows it on hover, keyboard focus, or tap.
  var tip = document.createElement('div'); tip.className = 'tip'; tip.hidden = true; document.body.appendChild(tip);
  var tapped = null;  // the element a tap opened, so the next tap on it closes again
  function place(el) {
    tip.textContent = el.getAttribute('data-tip'); tip.hidden = false;
    var r = el.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
    var left = Math.min(Math.max(8, r.left), Math.max(8, window.innerWidth - w - 8));
    var top = r.bottom + 6; if (top + h > window.innerHeight - 8) top = r.top - h - 6;
    tip.style.left = left + 'px'; tip.style.top = Math.max(8, top) + 'px';
  }
  function hide() { tip.hidden = true; tapped = null; }
  document.querySelectorAll('[data-tip]').forEach(function (el) {
    // touch pointers are handled by click alone: they also fire enter/focus, and toggling on
    // those would close the tip in the same tap that opened it.
    el.addEventListener('pointerenter', function (ev) { if (ev.pointerType !== 'touch') place(el); });
    el.addEventListener('pointerleave', function (ev) { if (ev.pointerType !== 'touch') hide(); });
    el.addEventListener('focus', function () { place(el); });
    el.addEventListener('blur', hide);
    el.addEventListener('click', function (ev) {
      if (tapped === el) { hide(); } else { place(el); tapped = el; }
      ev.stopPropagation();
    });
  });
  document.addEventListener('click', hide);
  document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') hide(); });
  // the tip is positioned against the viewport, so it must not linger once things move
  window.addEventListener('scroll', hide, true);
  window.addEventListener('resize', hide);
})();
(function () {
  var colors = ['--s1', '--s2', '--s3', '--s4'];
  var readouts = [];  // {el, hide} per chart, so a touch elsewhere closes the one left open
  document.addEventListener('pointerdown', function (ev) {
    readouts.forEach(function (r) { if (!r.el.contains(ev.target)) r.hide(); });
  }, true);
  document.querySelectorAll('.chart-wrap').forEach(function (wrap) {
    var data = JSON.parse(wrap.getAttribute('data-chart'));
    var tip = wrap.querySelector('.tooltip');
    // one wrap holds a wide and a narrow rendering of the same series; each has its own
    // geometry, and CSS displays exactly one, so wire up both and let events pick the winner.
    wrap.querySelectorAll('svg.chart').forEach(function (svg) {
      var xpos = JSON.parse(svg.getAttribute('data-xpos'));
      var narrow = svg.classList.contains('narrow');
      var hit = svg.querySelector('.hit'), cross = svg.querySelector('.crosshair');
      var vb = svg.viewBox.baseVal;
      function fmt(v) { if (v == null) return '-'; return v >= 0.01 ? v.toFixed(3) + ' s' : (v * 1000).toFixed(2) + ' ms'; }
      function show(ev) {
        var r = svg.getBoundingClientRect(), sx = vb.width / r.width;
        if (!r.width) return;
        var x = (ev.clientX - r.left) * sx, best = 0, bd = 1e9;
        xpos.forEach(function (px, i) { var d = Math.abs(px - x); if (d < bd) { bd = d; best = i; } });
        cross.setAttribute('x1', xpos[best]); cross.setAttribute('x2', xpos[best]);
        cross.setAttribute('visibility', 'visible');
        while (tip.firstChild) tip.removeChild(tip.firstChild);
        var hx = document.createElement('div'); hx.className = 't-x';
        hx.textContent = data.xs[best] + ' thread' + (data.xs[best] === 1 ? '' : 's'); tip.appendChild(hx);
        var css = getComputedStyle(document.documentElement);
        data.series.forEach(function (s, i) {
          var row = document.createElement('div'); row.className = 't-row';
          var l = document.createElement('span'); l.className = 't-l';
          var dot = document.createElement('i'); dot.style.background = css.getPropertyValue(colors[i % 4]);
          l.appendChild(dot); l.appendChild(document.createTextNode(narrow ? s.short : s.label));
          var v = document.createElement('strong'); v.textContent = fmt(s.vals[best]);
          var rng = s.rng && s.rng[best];
          if (rng && !narrow) { var sm = document.createElement('span'); sm.className = 't-r';
                                sm.textContent = ' (' + fmt(rng[0]) + ' – ' + fmt(rng[1]) + ')'; v.appendChild(sm); }
          row.appendChild(l); row.appendChild(v); tip.appendChild(row);
        });
        tip.hidden = false;
        var left = (xpos[best] / sx) + 12, top = (ev.clientY - r.top) - 10;
        if (left + tip.offsetWidth > r.width) left = (xpos[best] / sx) - tip.offsetWidth - 12;
        tip.style.left = Math.max(0, Math.min(left, r.width - tip.offsetWidth)) + 'px';
        tip.style.top = Math.max(0, Math.min(top, r.height - tip.offsetHeight)) + 'px';
      }
      function hide() { cross.setAttribute('visibility', 'hidden'); tip.hidden = true; }
      hit.addEventListener('pointermove', show);
      hit.addEventListener('pointerdown', show);   // a tap reads the nearest thread count
      // a touch reading stays up after the finger lifts (the document handler above closes
      // it); only a mouse leaving, or a gesture the browser took over, dismisses it directly.
      hit.addEventListener('pointerleave', function (ev) { if (ev.pointerType !== 'touch') hide(); });
      hit.addEventListener('pointercancel', hide);
      readouts.push({ el: svg, hide: hide });
    });
  });
})();
"""


def render_html(doc: dict) -> str:
    p = pivot(doc)
    configs = _configs_present(doc)
    cats = {w["name"]: w for w in doc["workloads"]}
    core = [w for w in doc["workloads"] if w["category"] != "library" and w["name"] in p]
    libs = [w for w in doc["workloads"] if w["category"] == "library" and w["name"] in p]

    findings = "".join(f"<li>{html.escape(f)}</li>" for f in key_findings(doc))

    core_html = ""
    for cat in ("cpu", "io", "contended", "multiprocessing"):
        ws = [w for w in core if w["category"] == cat]
        if not ws:
            continue
        core_html += (f'<section class="category"><h3>{CATEGORY_TITLES[cat]}</h3><p class="blurb">{CATEGORY_BLURB[cat]}</p>'
                      + "".join(_workload_block(w, p[w["name"]], doc, configs) for w in ws) + "</section>")

    libs_html = ""
    if libs:
        libs_html = (f'<h2 id="libs">Library workloads (optional tier)</h2><p class="blurb">{CATEGORY_BLURB["library"]}</p>'
                     + _support_table(doc.get("gil_support", {}))
                     + "".join(_workload_block(w, p[w["name"]], doc, configs) for w in libs))

    gil_warnings = "".join(
        f'<details><summary>{html.escape(c["label"])} / {html.escape(name)}</summary><pre>{html.escape(text)}</pre></details>'
        for c in doc["configs"] for name, text in (c.get("stderr") or {}).items())

    host = doc["host"]
    title = "Python 3.12 vs 3.14 vs 3.14t: GIL and free-threading benchmark"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<main>
<h1>{html.escape(title)}</h1>
<p class="sub">Generated {html.escape(doc['generated_at'])} on {html.escape(str(host.get('cpu')))} ({host.get('ncpu')} cores),
{html.escape(str(host.get('os')))}. {len(doc['records'])} measurements, median of {doc['repeats']} repeats.</p>
<nav class="toc"><a href="#findings">Key findings</a><a href="#summary">Summary</a><a href="#core">Bare Python</a>
{'<a href="#libs">Libraries</a>' if libs else ''}<a href="#impl">Implementation differences</a><a href="#method">Reproduce</a></nav>

<h2 id="findings">Key findings</h2>
<ul class="findings">{findings}</ul>

<h2 id="summary">Summary</h2>
<h4>Interpreter configurations</h4>
{_config_table(doc)}
<h4>Speed-up at a glance</h4>
{_summary_table(doc, p, configs)}

<h2 id="core">Bare Python workloads (stdlib only)</h2>
{core_html}
{libs_html}
{_impl_differences(doc)}
{_methodology(doc)}
{('<h3>Appendix: interpreter GIL warnings captured during the run</h3>' + gil_warnings) if gil_warnings else ''}
</main>
<script>{_JS}</script>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", default="results/results.json")
    ap.add_argument("--out", default="results/report.html")
    args = ap.parse_args(argv)
    doc = json.loads(Path(args.src).read_text())
    Path(args.out).write_text(render_html(doc))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
