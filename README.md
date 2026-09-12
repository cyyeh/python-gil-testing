# python-gil-testing

Benchmarks CPython **3.12** (GIL) against **3.14** with the GIL on and the
**free-threaded 3.14t** build (GIL off), and renders a self-contained HTML report
that also explains what changed inside the interpreter.

**Latest report:** https://cyyeh.github.io/python-gil-testing/ (GitHub Pages serves
`results/report.html` straight from `main`; re-run and push to update it).

Bare-Python (stdlib) workloads are the core tier. Library workloads
(numpy, pandas, duckdb, scikit-learn, fastapi) are an optional tier that goes
through a **free-threading support check first** - because a single C extension
that has not opted in silently re-enables the GIL for the whole process.

## Start here: the `gil-lib-compare` skill (any coding agent)

The library-comparison workflow ships as an [Agent Skill](https://agentskills.io) so
your coding agent follows the same gated flow every time. It is wired up for:

| agent | how it finds the skill |
|---|---|
| Claude Code | auto-discovers `.claude/skills/gil-lib-compare/` - type `/gil-lib-compare` or just ask |
| Codex CLI, GitHub Copilot, Cursor, Jules, Amp, ... | read `AGENTS.md`, which mandates `.agents/skills/gil-lib-compare/SKILL.md` |
| Gemini CLI | `GEMINI.md` imports `AGENTS.md` |
| anything else | tell it: *"follow `.agents/skills/gil-lib-compare/SKILL.md`"* |

Then ask in plain words:

```text
does duckdb support free-threading?
add polars to the GIL vs free-threaded comparison and tell me how it does
re-run the numpy comparison and regenerate the report
```

The skill makes the agent follow one gated flow, in this order:

1. install the library into **both** venvs, wheels only (`--no-build`; no `cp314t` wheel -> stop and tell you)
2. run `check_gil_support.py` on 3.14t **before** any benchmark and **tell you immediately** if the
   library re-enables the GIL (its "3.14t" numbers would otherwise be silently GIL-on)
3. add a workload to `bench/lib_workloads.py` if none exists - constant total work, deterministic
   checksum, lazy import, warm-up, the library's own thread pool pinned
4. run the tests on both interpreters
5. `python -m bench.compare_lib <pkg>` - re-check, warn, benchmark (5 timed repeats per cell by
   default), merge into `results/results.json`, regenerate `results/report.html`
6. report speed-ups per configuration with ± stdev, flag `[GIL re-enabled by import]`, link the report

The five libraries already in `results/report.html` (numpy, pandas, duckdb, scikit-learn, fastapi)
were produced through exactly this flow; duckdb is the example of a library that ships a
`cp314t` wheel but still re-enables the GIL.

`.agents/skills/...` is the canonical copy; `.claude/skills/...` mirrors it and
`tests/test_agent_files.py` fails if they drift.

Without an agent, the same flow by hand:

```bash
.venv314t/bin/python check_gil_support.py <pkg>          # 1-2: is it safe? (exit 1 = no)
.venv312/bin/python -m bench.compare_lib <pkg> --install # 3-6: install, check, bench, report
```

## Quick start

```bash
./run_all.sh            # install 3.12 + 3.14t with uv, benchmark stdlib workloads, write results/report.html
./run_all.sh --libs     # also benchmark the library tier
./run_all.sh --quick    # smoke run (tiny sizes, 1 repeat)
open results/report.html
```

Requires [`uv`](https://docs.astral.sh/uv/). Nothing is installed outside this
directory except uv-managed interpreters.

## Does library X support free-threading?

```bash
.venv314t/bin/python check_gil_support.py numpy pandas duckdb fastapi
```

```
package  version  wheel ABI  status         detail
numpy    2.5.3    cp314t     SUPPORTED      2 extension module(s) loaded, GIL stayed off
pandas   3.0.5    cp314t     SUPPORTED      55 extension module(s) loaded, GIL stayed off
duckdb   1.5.5    cp314t     REENABLES_GIL  '_duckdb' has not declared Py_MOD_GIL_NOT_USED, so CPython re-enabled the GIL ...
fastapi  0.141.1  pure       SUPPORTED      1 extension module(s) loaded, GIL stayed off
```

`check_gil_support.py` is a single stdlib-only file - copy it anywhere. It imports
each package in a fresh free-threaded subprocess and reports the wheel ABI tag,
the extension modules that got loaded, and whether CPython re-enabled the GIL
(`RuntimeWarning: The global interpreter lock (GIL) has been enabled to load
module ...`). Exit status 1 if anything re-enables the GIL or is missing;
`--json` for machine output; `-r requirements.txt` to check a whole project.

| status | meaning |
|---|---|
| `SUPPORTED` | compiled extensions loaded, GIL stayed off |
| `PURE_PYTHON` | no compiled extensions - runs GIL-free (thread-safety is still the library's job) |
| `REENABLES_GIL` | an extension module lacks `Py_MOD_GIL_NOT_USED`; the whole process falls back to the GIL |
| `NOT_INSTALLED` | import failed |
| `UNKNOWN` | not probed on a free-threaded interpreter, or GIL was already forced on |

## Compare one library end to end

```bash
.venv312/bin/python -m bench.compare_lib numpy            # check -> warn -> bench -> report
.venv312/bin/python -m bench.compare_lib polars --install # install into both venvs first
```

If the package re-enables the GIL you get a loud warning, its "3.14t" results are
flagged in the report, and an extra `3.14t (PYTHON_GIL=0 forced)` column is
measured for comparison only. If no workload uses the package yet, the command
prints the contract for adding one. This is the command the skill above drives.

## Layout

```
setup.sh / run_all.sh      install + run + report
index.html / .nojekyll     GitHub Pages entry point (redirects to results/report.html)
AGENTS.md / CLAUDE.md / GEMINI.md   agent instructions (all point at the skill)
.agents/skills/, .claude/skills/    the gil-lib-compare skill (canonical + Claude Code mirror)
check_gil_support.py       standalone free-threading support checker
bench/workloads.py         stdlib workloads: cpu_primes, cpu_float, io_sleep,
                           contended_list_append, contended_dict_update, mp_primes
bench/lib_workloads.py     library workloads: numpy_small_ops, numpy_matmul, pandas_groupby,
                           duckdb_query, sklearn_fit, fastapi_sync_cpu, fastapi_async_json
bench/worker.py            runs one workload at each thread count inside one interpreter
bench/run.py               drives every (config x workload) in its own subprocess -> results/results.json
bench/facts.py             measured build facts (ABI tag, flags, object sizes) per interpreter
bench/report.py            results.json -> results/report.html (inline SVG, no network)
bench/verify_report.py     re-derives every number on the page from the raw timings (exit 1 on a mismatch)
bench/compare_lib.py       one-command library comparison
tests/                     unittest suite; run on BOTH interpreters
```

## Interpreter configurations

| label | binary | GIL |
|---|---|---|
| `3.12` | cpython-3.12.13 | on (baseline) |
| `3.14 (GIL on)` | cpython-3.14.6+freethreaded, `PYTHON_GIL=1` | on - same binary, isolates the GIL's effect |
| `3.14t (free-threaded)` | cpython-3.14.6+freethreaded | off |
| `3.14t (PYTHON_GIL=0 forced)` | same, `PYTHON_GIL=0` | off, only for workloads whose library re-enables it |

## Workload contract

Every workload is `fn(n_workers, **sizes) -> value`. Total work is independent
of `n_workers` and the return value must be identical at every thread count -
`tests/` enforce this, so each run provably does the same work. Each
(config, workload) pair runs in a fresh subprocess; BLAS/OpenMP are pinned to one thread.

## Repeats and statistics

Every (configuration, workload, thread count) cell is timed **5 times** by default;
override with `--repeats N` on `bench.run` / `bench.compare_lib` (use 7-10 for
sub-50 ms workloads). The worker records median, mean, sample standard deviation,
min, max and coefficient of variation per cell (`results/results.json` -> `stats`).
The report plots the median with min-max error bars, tables show
median ± stdev (hover for min/max), cells with cv > 10% are marked `~`, and the
"Key findings" list opens with a noise summary. Differences smaller than the error
bars are noise, not findings.

## Portability

Everything is stdlib Python driving `uv`-managed interpreters, so it runs on
macOS, Linux and Windows (uv publishes `cpython-3.12` and `cpython-3.14+freethreaded`
builds for all three; free-threaded `cp314t` wheels for numpy, pandas,
scikit-learn and duckdb exist for the mainstream platforms - `check_gil_support.py`
tells you if one is missing).

* **macOS / Linux:** `./run_all.sh` as above.
* **Windows:** run `setup.sh` / `run_all.sh` from Git Bash or WSL, or do the four
  lines by hand in PowerShell:
  ```powershell
  uv python install cpython-3.12.13 cpython-3.14.6+freethreaded
  uv venv --python cpython-3.12.13 .venv312; uv venv --python cpython-3.14.6+freethreaded .venv314t
  .venv312\Scripts\python.exe -m bench.run          # defaults find Scripts\python.exe automatically
  .venv312\Scripts\python.exe -m bench.report          # -> results\report.html
  ```
* Thread counts default to this machine's: 1, doubling, up to the usable core count (capped at
  6 points, so a 64-core box does not multiply the run time). Adding to an existing
  `results.json` reuses the counts already in it. `--workers 1,2,4,8,16` overrides both.
  Absolute numbers depend on the CPU; the *shapes* (flat on GIL builds, scaling on 3.14t) do not.
* `--install` uses `uv pip install --no-build`, so a library without a `cp314t` wheel fails
  fast instead of starting a long source build.
* Network access is only needed for the first install.

## Tests

```bash
.venv312/bin/python  -m unittest discover -s tests -v
.venv314t/bin/python -m unittest discover -s tests -v
```

## License

MIT - see [LICENSE](LICENSE).
