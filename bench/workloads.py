"""Benchmark workloads. Pure stdlib so the same file runs unchanged on every
interpreter under test.

Every workload takes ``n_workers`` as its first argument and returns a value
that must be identical regardless of ``n_workers`` -- the harness uses that to
prove each run did the same amount of work.
"""

from __future__ import annotations

import math
import multiprocessing
import threading
import time
from dataclasses import dataclass
from typing import Callable

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def split_range(lo: int, hi: int, n: int) -> list[tuple[int, int]]:
    """Split [lo, hi) into ``n`` contiguous chunks; earlier chunks get the remainder."""
    size, extra = divmod(hi - lo, n)
    chunks = []
    start = lo
    for i in range(n):
        end = start + size + (1 if i < extra else 0)
        chunks.append((start, end))
        start = end
    return chunks


def _run_threads(target: Callable, args_per_thread: list[tuple]) -> list:
    """Run ``target`` once per args tuple on its own thread; return results in order."""
    results: list = [None] * len(args_per_thread)

    def wrapper(i: int, args: tuple) -> None:
        results[i] = target(*args)

    threads = [
        threading.Thread(target=wrapper, args=(i, a)) for i, a in enumerate(args_per_thread)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


# --------------------------------------------------------------------------
# CPU-bound
# --------------------------------------------------------------------------


def count_primes(lo: int, hi: int) -> int:
    """Trial-division prime count over [lo, hi). Deliberately naive: pure bytecode."""
    count = 0
    for n in range(max(lo, 2), hi):
        if n % 2 == 0:
            count += n == 2
            continue
        limit = math.isqrt(n)
        d = 3
        is_prime = True
        while d <= limit:
            if n % d == 0:
                is_prime = False
                break
            d += 2
        count += is_prime
    return count


def cpu_primes(n_workers: int, limit: int = 300_000) -> int:
    """Count primes below ``limit``, range split evenly across threads."""
    return sum(_run_threads(count_primes, split_range(2, limit, n_workers)))


def _float_kernel(lo: int, hi: int) -> float:
    acc = 0.0
    for i in range(lo, hi):
        acc += math.sin(i * 0.001) * math.cos(i * 0.002)
    return acc


def cpu_float(n_workers: int, iterations: int = 3_000_000) -> float:
    """Float/trig accumulation, range split across threads."""
    return math.fsum(_run_threads(_float_kernel, split_range(0, iterations, n_workers)))


# --------------------------------------------------------------------------
# I/O-bound (simulated with sleep; the GIL is released during sleep)
# --------------------------------------------------------------------------


def _sleep_kernel(waits: int, seconds: float) -> int:
    for _ in range(waits):
        time.sleep(seconds)
    return waits


def io_sleep(n_workers: int, waits: int = 20, seconds: float = 0.01) -> int:
    """Each thread performs ``waits`` sleeps; returns total waits performed."""
    return sum(_run_threads(_sleep_kernel, [(waits, seconds)] * n_workers))


# --------------------------------------------------------------------------
# Contended shared state (exercises per-object locking on free-threaded builds)
# --------------------------------------------------------------------------


def contended_list_append(n_workers: int, total: int = 4_000_000) -> int:
    """``total`` appends to ONE shared list, split across threads; returns final length."""
    shared: list[int] = []

    def kernel(lo: int, hi: int) -> None:
        append = shared.append
        for i in range(lo, hi):
            append(i)

    _run_threads(kernel, split_range(0, total, n_workers))
    return len(shared)


def contended_dict_update(n_workers: int, total: int = 2_000_000, keys: int = 64) -> int:
    """``total`` counter increments in ONE shared dict under a lock, split across threads;
    returns total increments."""
    shared: dict[int, int] = {k: 0 for k in range(keys)}
    lock = threading.Lock()

    def kernel(lo: int, hi: int) -> None:
        for i in range(lo, hi):
            k = i % keys
            with lock:
                shared[k] += 1

    _run_threads(kernel, split_range(0, total, n_workers))
    return sum(shared.values())


# --------------------------------------------------------------------------
# multiprocessing reference (the traditional way around the GIL)
# --------------------------------------------------------------------------


def mp_primes(n_workers: int, limit: int = 300_000) -> int:
    """Same as cpu_primes but each chunk runs in its own process."""
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(n_workers) as pool:
        return sum(pool.starmap(count_primes, split_range(2, limit, n_workers)))


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Workload:
    name: str
    category: str  # cpu | io | contended | multiprocessing
    fn: Callable[[int], object]
    description: str
    requires: tuple[str, ...] = ()  # importable third-party packages needed
    warmup: bool = True  # run fn(1) once untimed first (cold caches, lazy imports, server start-up)


WORKLOADS: list[Workload] = [
    Workload(
        "cpu_primes",
        "cpu",
        cpu_primes,
        "Trial-division prime counting below 300k, range split across threads. "
        "Pure bytecode: integer arithmetic and loops, no I/O.",
    ),
    Workload(
        "cpu_float",
        "cpu",
        cpu_float,
        "3M iterations of sin*cos accumulation, range split across threads. "
        "Float-heavy bytecode with C-level math calls.",
    ),
    Workload(
        "io_sleep",
        "io",
        io_sleep,
        "Each thread performs 20 x 10ms sleeps (simulated network/disk waits). "
        "The GIL is released while sleeping, so all builds should overlap waits.",
    ),
    Workload(
        "contended_list_append",
        "contended",
        contended_list_append,
        "4M appends to one shared list, split across threads. On free-threaded builds each append "
        "takes the list's per-object critical section, so more threads means more lock hand-offs.",
    ),
    Workload(
        "contended_dict_update",
        "contended",
        contended_dict_update,
        "2M dict increments guarded by one threading.Lock, split across threads. "
        "Measures lock hand-off cost when the GIL is not serialising threads.",
    ),
    Workload(
        "mp_primes",
        "multiprocessing",
        mp_primes,
        "Same prime counting as cpu_primes, but using a multiprocessing.Pool "
        "(spawn). The classic GIL workaround; includes process start-up cost.",
    ),
]

WORKLOADS_BY_NAME = {spec.name: spec for spec in WORKLOADS}
