"""Check results/report.html against results/results.json.

Every number on the page is recomputed here from the raw per-repeat ``seconds`` - on
purpose *without* importing bench.report, so a bug in the renderer cannot hide behind its
own arithmetic. Exits non-zero on any mismatch.

What it cannot do is read: category blurbs, workload descriptions, the implementation
-differences section and the tooltips are hand-written, and a sentence that contradicts the
chart above it still needs a reader. The work-invariance section at the end exists to give
that reader the facts those sentences are usually wrong about.

    python -m bench.verify_report [--in results/results.json] [--page results/report.html]
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import statistics as st
from pathlib import Path

CONFIG_ORDER = ["3.12", "3.14 (GIL on)", "3.14t (free-threaded)", "3.14t (PYTHON_GIL=0 forced)"]
FT = "3.14t (free-threaded)"
NOISY_CV = 0.10
FLAG = "⚠"


# --------------------------------------------------------------------------
# tiny HTML helpers (the page is generated, so its shape is known)
# --------------------------------------------------------------------------


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment))


def _tight(fragment: str) -> str:
    return re.sub(r"\s+", " ", _text(fragment)).strip()


def _label(cell: str) -> str:
    """A row/column header's full configuration label (narrow screens also carry a short one)."""
    m = re.search(r'<span class="only-wide">(.*?)</span>', cell, re.S)
    return _tight(m.group(1) if m else cell)


def _section(page: str, start: str, end: str = "</table>") -> str:
    i = page.index(start)
    return page[i:page.index(end, i)]


def _rows(table: str) -> list[list[str]]:
    return [re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S) for r in re.findall(r"<tr>(.*?)</tr>", table, re.S)]


# --------------------------------------------------------------------------
# formatting, duplicated from the renderer on purpose (an independent copy)
# --------------------------------------------------------------------------


def fmt_s(v: float) -> str:
    return f"{v:.3f} s" if v >= 0.01 else f"{v * 1000:.2f} ms"


def fmt_x(v: float) -> str:
    return f"{v:.1f}x"


def ticks_top(vmax: float, n: int = 4) -> float:
    raw = vmax / n
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if step >= raw:
            break
    t = 0.0
    while t <= vmax + 1e-12:
        t += step
    return round(t, 10)


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------


def pivot(doc: dict) -> dict:
    out: dict = {}
    for r in doc["records"]:
        if r.get("workers"):
            out.setdefault(r["workload"], {}).setdefault(r["config"], {})[r["workers"]] = r
    return out


def configs_present(doc: dict) -> list[str]:
    present = {r["config"] for r in doc["records"]}
    return [c for c in CONFIG_ORDER if c in present] + sorted(present - set(CONFIG_ORDER))


def check_stats(doc: dict, page: str = "") -> tuple[list[str], int]:
    """The stored median/stats must still be what the raw seconds say."""
    bad, n = [], 0
    for r in doc["records"]:
        s = r.get("seconds")
        if not s:
            continue
        mean = st.fmean(s)
        sd = st.stdev(s) if len(s) > 1 else 0.0
        want = {"median": st.median(s), "mean": mean, "stdev": sd, "min": min(s), "max": max(s),
                "cv": sd / mean if mean else 0.0, "n": len(s)}
        where = f"{r['workload']}/{r['config']}/{r['workers']}thr"
        if abs(r["median"] - want["median"]) > 1e-9:
            bad.append(f"{where}: stored median {r['median']} != {want['median']} from seconds")
        for k, v in want.items():
            n += 1
            if abs(r["stats"][k] - v) > 1e-9:
                bad.append(f"{where}: stats[{k}] {r['stats'][k]} != {v} from seconds")
    return bad, n


def check_tables(doc: dict, page: str) -> tuple[list[str], int]:
    p, bad, n = pivot(doc), [], 0
    mx = max(doc["workers"])

    for row in _rows(_section(page, '<table class="data summary">'))[1:]:
        name = _tight(row[0]).split()[0]
        for cfg, cell in zip(configs_present(doc), row[1:]):
            bw, shown = p[name].get(cfg), _tight(cell)
            n += 1
            if not bw:
                if shown != "-":
                    bad.append(f"summary {name}/{cfg}: page shows {shown!r} with no record behind it")
                continue
            ws = sorted(bw)
            want = f"{fmt_x(st.median(bw[ws[0]]['seconds']) / st.median(bw[ws[-1]]['seconds']))} {fmt_s(st.median(bw[mx]['seconds']))}"
            if cfg == FT and bw[mx].get("gil_enabled"):
                want += " " + FLAG
            if shown != want:
                bad.append(f"summary {name}/{cfg}: page={shown!r} recomputed={want!r}")

    for name, by_cfg in p.items():
        block = _section(page, f'id="w-{name}"', "</article>")
        for row in _rows(_section(block, '<table class="data">'))[1:]:
            cfg = _label(row[0])
            if cfg not in by_cfg:
                bad.append(f"{name}: table row labelled {cfg!r}, which is not a configuration in the data")
                continue
            for w, cell in zip(doc["workers"], row[1:-1]):
                rec = by_cfg[cfg].get(w)
                n += 1
                if rec is None:
                    continue
                s = rec["seconds"]
                sd = st.stdev(s)
                want = f"{fmt_s(st.median(s))} ±{fmt_s(sd)}"
                if sd / st.fmean(s) > NOISY_CV:
                    want += " ~"
                if cfg == FT and rec.get("gil_enabled"):
                    want += " " + FLAG
                if _tight(cell) != want:
                    bad.append(f"{name}/{cfg}/{w}thr: page={_tight(cell)!r} recomputed={want!r}")
            ws = sorted(by_cfg[cfg])
            want_sp = fmt_x(st.median(by_cfg[cfg][ws[0]]["seconds"]) / st.median(by_cfg[cfg][ws[-1]]["seconds"]))
            n += 1
            if _tight(row[-1]) != want_sp:
                bad.append(f"{name}/{cfg} speed-up: page={_tight(row[-1])!r} recomputed={want_sp!r}")
    return bad, n


# viewBox and plot margins of the two renderings, which must plot the same numbers
GEOMS = {"wide": {"H": 320, "mt": 36, "mb": 44}, "narrow": {"H": 292, "mt": 44, "mb": 44}}


def check_charts(doc: dict, page: str) -> tuple[list[str], int]:
    """Invert every plotted vertex back through the axis and land on the median again.

    The line vertices are read rather than the markers: a marker is four shapes with four
    different anchor points, while ``d="M x y L x y"`` is the value itself, for every series.
    """
    p, bad, n = pivot(doc), [], 0
    for name, by_cfg in p.items():
        block = _section(page, f'id="w-{name}"', "</article>")
        vals = [f(r["seconds"]) for bw in by_cfg.values() for r in bw.values() for f in (st.median, max)]
        top = ticks_top(max(vals))
        for variant, g in GEOMS.items():
            svg = _section(block, f'<svg class="chart {variant}"', "</svg>")
            ph = g["H"] - g["mt"] - g["mb"]
            for i, cfg in enumerate(c for c in configs_present(doc) if c in by_cfg):
                cls = f"s{i + 1}"
                want = [st.median(by_cfg[cfg][w]["seconds"]) for w in doc["workers"] if w in by_cfg[cfg]]
                ys = [float(y) for seg in re.findall(rf'<path class="series-line {cls}" d="([^"]+)"', svg)
                      for _, y in re.findall(r"([\d.]+) ([\d.]+)", seg)]
                markers = len(re.findall(rf'class="marker {cls}"', svg))
                n += 1
                if len(ys) != len(want) or markers != len(want):
                    bad.append(f"chart {name}/{variant}/{cfg}: {len(ys)} line vertices and {markers} markers "
                               f"for {len(want)} measured points")
                    continue
                for w, y, v in zip(doc["workers"], ys, want):
                    n += 1
                    plotted = (g["mt"] + ph - y) / ph * top
                    if abs(plotted - v) > 0.2 / ph * top:      # the page rounds to 0.1 user units
                        bad.append(f"chart {name}/{variant}/{cfg}/{w}thr: plots {plotted:.4f}s, median is {v:.4f}s")
    return bad, n


def check_metadata(doc: dict, page: str) -> tuple[list[str], int]:
    bad, n = [], 0
    host = doc["host"]
    sub = _tight(re.search(r'<p class="sub">(.*?)</p>', page, re.S).group(1))
    for frag in (doc["generated_at"], str(host.get("cpu")), f"({host.get('ncpu')} cores)", str(host.get("os")),
                 f"{len(doc['records'])} measurements", f"median of {doc['repeats']} repeats"):
        n += 1
        if frag not in sub:
            bad.append(f"header line is missing {frag!r}")

    rows = _rows(_section(page, "Interpreter configurations"))[1:]
    for cfg, row in zip(doc["configs"], rows):
        info = cfg.get("info") or {}
        want = [str(info.get("version", "?")), "yes" if info.get("free_threaded_build") else "no",
                "yes" if info.get("gil_enabled", True) else "no",
                " ".join(f"{k}={v}" for k, v in cfg.get("env", {}).items() if k == "PYTHON_GIL") or "-",
                cfg.get("note", "")]
        got = [_tight(c) for c in row[1:]]
        n += 1
        if got != [_tight(w) for w in want]:
            bad.append(f"config row {cfg['label']}: page={got} data={want}")

    facts_cfgs = [c for c in doc["configs"] if c.get("facts")]
    if facts_cfgs:
        keys = ["version", "abiflags", "soabi", "free_threaded_build", "gil_enabled", "gil_flag",
                "thread_inherit_context", "context_aware_warnings", "jit_available", "gc_threshold", "compiler"]
        rows = _rows(_section(page, '<table class="data facts">'))[1:]
        for key, row in zip(keys, rows):
            n += 1
            got = [_tight(c) for c in row[1:]]
            want = [_tight(str(c["facts"].get(key))) for c in facts_cfgs]
            if got != want:
                bad.append(f"facts row {key}: page={got} data={want}")
        for row in rows[len(keys):]:
            name = html.unescape(re.search(r"<code>(.*?)</code>", row[0]).group(1))
            n += 1
            got = [_tight(c) for c in row[1:]]
            want = [str(c["facts"]["sizeof"].get(name, "-")) for c in facts_cfgs]
            if got != want:
                bad.append(f"facts row sizeof({name}): page={got} data={want}")

    if doc.get("gil_support"):
        rows = _rows(_section(page, '<table class="data support">'))[1:]
        for (pkg, e), row in zip(doc["gil_support"].items(), rows):
            got = " ".join(_tight(c) for c in row)
            for frag in (pkg, str(e.get("version") or "-"), str(e.get("wheel_abi") or "-"), e["status"], e["detail"]):
                n += 1
                if _tight(frag) not in got:
                    bad.append(f"support row {pkg}: missing {frag!r}")
    return bad, n


def check_findings(doc: dict, page: str) -> tuple[list[str], int]:
    """The generated bullets: per-workload speed-ups and the noise summary."""
    p, bad, n = pivot(doc), [], 0
    findings = [_tight(li) for li in re.findall(r"<li>(.*?)</li>", _section(page, '<ul class="findings">', "</ul>"), re.S)]
    for line in findings:
        m = re.match(r"(?:CPU-bound|Library) (\w+) at \d+ threads vs 1 - (.*)\.$", line)
        if m:
            name, rest = m.group(1), m.group(2)
            want = ", ".join(f"{c}: {fmt_x(st.median(p[name][c][min(p[name][c])]['seconds']) / st.median(p[name][c][max(p[name][c])]['seconds']))}"
                             for c in configs_present(doc) if c in p[name])
            n += 1
            if rest != want:
                bad.append(f"finding for {name}: page={rest!r} recomputed={want!r}")
        if line.startswith("Measurement noise:"):
            cvs = [st.stdev(r["seconds"]) / st.fmean(r["seconds"]) for r in doc["records"] if r.get("seconds")]
            ns = {len(r["seconds"]) for r in doc["records"] if r.get("seconds")}
            want = (f"{min(ns)}{'-' + str(max(ns)) if len(ns) > 1 else ''} timed repeats per cell, "
                    f"median coefficient of variation {st.median(cvs) * 100:.1f}%; "
                    f"{sum(1 for c in cvs if c > NOISY_CV)} of {len(cvs)} cells exceed {NOISY_CV:.0%}")
            n += 1
            if want not in line:
                bad.append(f"noise finding: page={line!r} recomputed={want!r}")
    if not n:
        bad.append("no generated findings could be matched - has their wording changed?")
    return bad, n


def work_invariance(doc: dict) -> list[str]:
    """Which workloads really do keep their total work constant across thread counts.

    Not a pass/fail: a workload may fix work *per thread* on purpose. It is reported because
    the page's prose tends to claim otherwise, and because a checksum that drifts silently is
    the one bug these timings cannot show.
    """
    out = []
    for name in sorted({r["workload"] for r in doc["records"]}):
        vals = {r["result"] for r in doc["records"] if r["workload"] == name and r.get("result") is not None}
        if len(vals) <= 1:
            continue
        try:
            nums = sorted(float(v) for v in vals)
        except ValueError:
            out.append(f"{name}: {len(vals)} different checksums {sorted(vals)[:3]}")
            continue
        if nums[0] and (nums[-1] - nums[0]) / abs(nums[0]) < 1e-9:
            out.append(f"{name}: identical to ~1e-9 but not exactly ({len(vals)} values) - float summation order")
        else:
            out.append(f"{name}: total work VARIES with thread count - {sorted(vals)[:4]}")
    return out


def verify(doc: dict, page: str) -> tuple[list[str], list[str]]:
    problems, counts = [], []
    for title, fn in (("stored statistics vs raw timings", check_stats),
                      ("rendered table cells", check_tables),
                      ("plotted chart markers", check_charts),
                      ("header, config, facts and support tables", check_metadata),
                      ("generated key findings", check_findings)):
        try:
            bad, n = fn(doc, page)
        except (ValueError, AttributeError, KeyError, IndexError) as exc:
            bad, n = [f"could not check {title}: the page's structure is not what was expected ({exc!r})"], 0
        problems += bad
        counts.append(f"  {title:45} {n:5} checked  {'OK' if not bad else str(len(bad)) + ' MISMATCH'}")
    return problems, counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="src", default="results/results.json")
    ap.add_argument("--page", default="results/report.html")
    args = ap.parse_args(argv)

    doc = json.loads(Path(args.src).read_text())
    page = Path(args.page).read_text()
    problems, counts = verify(doc, page)

    print(f"{args.page} vs {args.src}")
    print("\n".join(counts))
    notes = work_invariance(doc)
    if notes:
        print("  work per thread count (for reading the prose, not a failure):")
        for note in notes:
            print(f"    - {note}")
    if problems:
        print(f"\n{len(problems)} MISMATCH(ES) - the page does not say what the data says:")
        for x in problems:
            print(f"  - {x}")
        print("\nFix the renderer (or re-run the benchmark), regenerate, and run this again.")
        return 1
    print("\nOK: every rendered number reproduces from the raw per-repeat timings.")
    print("Prose is not checked here - re-read the page's hand-written text against these numbers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
