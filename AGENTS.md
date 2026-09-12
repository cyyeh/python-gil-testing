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
- Tests: `.venv312/bin/python -m unittest discover -s tests` and again with `.venv314t`.
  Every workload must return an identical value at every thread count.
- Code is stdlib-only except inside `bench/lib_workloads.py` (lazy imports there).
- Do not cite a single run or a difference smaller than its error bars as a finding.
