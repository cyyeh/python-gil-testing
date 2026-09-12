# Instructions for coding agents

This repo benchmarks CPython 3.12 (GIL) vs 3.14 (GIL on) vs free-threaded 3.14t and
renders `results/report.html`. Two interpreters live in `.venv312/` and `.venv314t/`
(create them with `./setup.sh`; on Windows see README "Portability").

## Any task that involves a third-party library

Read and follow `.agents/skills/gil-lib-compare/SKILL.md` **before** doing anything
else ("does X support free-threading?", "add / benchmark / compare library X",
"re-run the numpy comparison"). Its gates are mandatory, in order:

1. `python -m bench.compare_lib <pkg> --install` - wheels only; no `cp314t` wheel -> stop, tell the user
2. `.venv314t/bin/python check_gil_support.py <pkg>` - **before** any benchmark; if the
   status is `REENABLES_GIL`, tell the user immediately (the library's "3.14t" numbers are GIL-on)
3. add a workload to `bench/lib_workloads.py` if none exists (contract is in the skill)
4. run the tests on **both** interpreters
5. `python -m bench.compare_lib <pkg>` (check -> warn -> bench -> merge -> report), 5 repeats by default
6. report speed-ups per configuration **with ± stdev**, flag `[GIL re-enabled by import]`, link the report

The same file is mirrored at `.claude/skills/gil-lib-compare/SKILL.md` for Claude Code;
`tests/test_agent_files.py` fails if the two copies differ - edit `.agents/...` and copy.

## Everything else

- Bare-Python benchmark: `.venv312/bin/python -m bench.run` (`--libs` adds the library tier,
  `--quick` for a smoke run, `--repeats N` to override the default of 5). Report:
  `.venv312/bin/python -m bench.report` -> `results/report.html`.
- **Never ship a report you have not verified.** `.venv312/bin/python -m bench.verify_report`
  recomputes every rendered number from the raw per-repeat timings and exits non-zero on a
  mismatch. It cannot read prose, so also check the page's hand-written text against the numbers
  now on it - a description that contradicts the chart under it is the failure mode this repo
  keeps hitting. Step 6 of the skill has the full loop, including handing the reading to a
  subagent. Descriptions are snapshotted into `results/results.json` at run time: fixing one in
  the registry also means updating it there (or re-running) before it reaches the page.
- Tests: `.venv312/bin/python -m unittest discover -s tests` and again with `.venv314t`.
  Every workload must return an identical value at every thread count.
- Code is stdlib-only except inside `bench/lib_workloads.py` (lazy imports there).
- Do not cite a single run or a difference smaller than its error bars as a finding.

## Rules for any HTML this repo generates

### A symbol never stands alone

Every glyph, badge, flag, abbreviation or marker
(`~`, `⚠`, `±`, status badges, column headers such as "speed-up") must explain itself
on hover, keyboard focus and tap: wrap it with `bench.report.tip(inner, explanation, cls)`,
which emits `data-tip` + `tabindex="0"` and is styled/handled by the page's own
tooltip runtime. Do not use the native `title` attribute (delayed, unstyled, invisible on
touch). `tests/test_report.py::HoverExplanationTest` enforces this on the rendered page;
extend `GLYPH_CLASSES` there when you introduce a new kind of marker.

### The page reads on a phone

The page itself never scrolls sideways, at any width down to 320px.

- **Tables** go through `bench.report._scroll(...)`. Cells stay on one line (only `.detail`
  columns wrap) and the table scrolls inside its own container, with the first column pinned
  and configuration labels abbreviated (`_config_label`) on narrow screens.
- **Charts** go through `bench.report.chart_block(...)`, which renders every chart twice:
  `WIDE` for pointer-sized screens and `NARROW` with its own margins, fonts and markers for
  phones; the stylesheet shows exactly one. Do not replace this with a single scaled SVG -
  a 680px viewBox at phone width renders its 11px type at ~5px, and CSS can restyle SVG text
  but not re-place it.
- **Touch** is a first-class pointer: a tap must open a tip and a second tap close it (the
  hover handlers sit out `pointerType === 'touch'`), and the chart readout survives the
  finger lifting.

`tests/test_report.py::MobileResponsiveTest` enforces the structure; check any visual change
at ~375px wide as well as on a desktop viewport.
