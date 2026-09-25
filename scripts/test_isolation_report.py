#!/usr/bin/env python3
"""Does a whole-directory pytest run agree with running the files one by one?

"Test isolation holds" is defined as: **the set of failing tests from a
single-process, multi-file run is a subset of the set from running each file
in its own process.** Anything in ``A - B`` failed only because an earlier
file left process-level state behind — the file passes on its own, so the
failure is a leak, not a bug in the test.

This script computes that difference:

  A = run ``pytest <dir>`` once, in ONE process  (``--full-timeout``)
  B = run ``pytest <file>`` per file, in parallel subprocesses
  A - B  → the pollution set (want: empty)

It is read-only with respect to the test code, but NOT with respect to the
checkout: ``tests/hermes_cli`` and friends shell out to real ``git``, and a
few of those call sites run ``git fetch`` / ``git checkout <branch>`` /
``git reset --hard`` against ``PROJECT_ROOT`` — which is the checkout this
script runs pytest in. Run it from a THROWAWAY worktree, never in the tree
holding your uncommitted work:

    git worktree add --detach /tmp/iso-run HEAD
    cd /tmp/iso-run && python scripts/test_isolation_report.py tests/hermes_cli

Two runs are useful for a before/after comparison — check out the baseline in
another worktree rather than flipping the working tree back and forth:

    scripts/test_isolation_report.py tests/hermes_cli --out before.json
    scripts/test_isolation_report.py tests/hermes_cli --out after.json

Usage:
    scripts/test_isolation_report.py [--dirs D [D ...]] [--workers N]
                                     [--full-timeout SEC] [--file-timeout SEC]
                                     [--skip-full] [--skip-per-file]
                                     [--out FILE] [--baseline FILE]

Exit code: 0 when ``A - B`` is empty, 1 otherwise (so it can gate CI later).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# pytest prints one line per failing/erroring node: "FAILED path::test_name - ..."
_RESULT_RE = re.compile(r"^(FAILED|ERROR)\s+(\S+)")
_SUMMARY_RE = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed)")


def _pytest_cmd(targets: list[str], extra: list[str]) -> list[str]:
    return [
        sys.executable, "-m", "pytest", *targets, "-q",
        "-p", "no:cacheprovider", "--color=no", *extra,
    ]


def _signal_group(proc: "subprocess.Popen", sig: int) -> None:
    """Send ``sig`` to the whole pytest process group (never raises)."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except OSError:
        try:
            proc.send_signal(sig)
        except OSError:
            pass


def _run(targets: list[str], timeout: float, extra: list[str]) -> dict:
    """Run pytest on ``targets``; return {'failed': set, 'timed_out': bool, ...}.

    A multi-file run can hang on leaked state, so ``timeout`` is a real
    possibility. It must NOT be enforced with ``subprocess.run``: that
    SIGKILLs pytest, and the ``FAILED`` lines this script parses are printed
    in pytest's short test summary — which never happens on SIGKILL. The
    result would be an EMPTY ``A`` and a bogus "isolation holds" verdict.
    Send SIGINT instead: pytest catches KeyboardInterrupt, prints the summary,
    and exits (this is what ``Ctrl-C`` does interactively).
    """
    cmd = _pytest_cmd(targets, extra)
    proc = subprocess.Popen(
        cmd, cwd=str(REPO_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, start_new_session=True,
    )
    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _signal_group(proc, signal.SIGINT)  # let pytest flush its summary
        try:
            out, _ = proc.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            _signal_group(proc, signal.SIGKILL)
            out, _ = proc.communicate()
    rc = proc.returncode
    failed, summary = set(), {}
    for line in out.splitlines():
        m = _RESULT_RE.match(line.strip())
        if m:
            failed.add(m.group(2))
        for m in _SUMMARY_RE.finditer(line):
            summary[m.group(2)] = summary.get(m.group(2), 0) + int(m.group(1))
    return {"failed": failed, "timed_out": timed_out, "returncode": rc, "summary": summary}


def _discover(directory: str) -> list[str]:
    root = REPO_ROOT / directory
    files = []
    for p in root.rglob("test_*.py"):
        try:
            files.append(str(p.relative_to(REPO_ROOT)))
        except ValueError:  # a directory outside the checkout (e.g. a probe tree)
            files.append(str(p))
    return sorted(files)


def _per_file(files: list[str], workers: int, timeout: float, extra: list[str]) -> tuple[set, list]:
    failed: set = set()
    problems: list = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for path, res in zip(files, pool.map(lambda f: _run([f], timeout, extra), files)):
            failed |= res["failed"]
            if res["timed_out"]:
                problems.append({"file": path, "problem": "timeout"})
            elif res["returncode"] not in (0, 1):
                problems.append({"file": path, "problem": f"exit {res['returncode']}"})
    return failed, problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dirs", nargs="+", default=["tests/hermes_cli", "tests/tools", "tests/agent"])
    ap.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 4))
    ap.add_argument("--full-timeout", type=float, default=2400.0, help="seconds for the single-process run (it can hang)")
    ap.add_argument("--file-timeout", type=float, default=600.0)
    ap.add_argument("--skip-full", action="store_true")
    ap.add_argument("--skip-per-file", action="store_true")
    ap.add_argument("--out", default="")
    ap.add_argument("--baseline", default="", help="compare against a previous --out JSON")
    ap.add_argument("--extra", default="", help="extra args passed to every pytest run")
    args = ap.parse_args()
    extra = args.extra.split() if args.extra else []

    report: dict = {"dirs": {}, "workers": args.workers}
    for directory in args.dirs:
        files = _discover(directory)
        print(f"\n=== {directory}: {len(files)} files ===", flush=True)
        entry: dict = {"files": len(files)}
        if not args.skip_full:
            print(f"  [A] single-process pytest {directory} (timeout {args.full_timeout:.0f}s) …", flush=True)
            full = _run([directory], args.full_timeout, extra)
            entry["A_full"] = {"count": len(full["failed"]), "timed_out": full["timed_out"], "summary": full["summary"]}
            entry["_A"] = sorted(full["failed"])
            print(f"      A={len(full['failed'])} failing{'(TIMED OUT — partial)' if full['timed_out'] else ''} {full['summary']}", flush=True)
        if not args.skip_per_file:
            print(f"  [B] per-file, {args.workers} workers …", flush=True)
            per_file, problems = _per_file(files, args.workers, args.file_timeout, extra)
            entry["B_per_file"] = {"count": len(per_file), "problems": problems}
            entry["_B"] = sorted(per_file)
            print(f"      B={len(per_file)} failing ({len(problems)} files timed out/errored)", flush=True)
        a, b = set(entry.get("_A", [])), set(entry.get("_B", []))
        if "_A" in entry and "_B" in entry:
            entry["A_minus_B"] = sorted(a - b)
            entry["B_minus_A"] = sorted(b - a)
            entry["A_and_B"] = sorted(a & b)
            print(f"      A-B (pollution) = {len(entry['A_minus_B'])}   "
                  f"A∩B (genuine) = {len(entry['A_and_B'])}   B-A = {len(entry['B_minus_A'])}", flush=True)
            for nid in entry["A_minus_B"]:
                print(f"        poll: {nid}", flush=True)
        report["dirs"][directory] = entry

    if args.baseline:
        base = json.loads(Path(args.baseline).read_text())
        print("\n=== before/after ===")
        for directory, entry in report["dirs"].items():
            was = set(base.get("dirs", {}).get(directory, {}).get("A_minus_B", []))
            now = set(entry.get("A_minus_B", []))
            print(f"  {directory}: before A-B={len(was)}  after A-B={len(now)}  fixed={len(was - now)}  new={len(now - was)}")
            for nid in sorted(now - was):
                print(f"      STILL/ NEW: {nid}")

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nwrote {args.out}")

    polluted = sum(len(e.get("A_minus_B", [])) for e in report["dirs"].values())
    print(f"\nTOTAL A-B (pollution-caused failures) = {polluted}")
    return 0 if polluted == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
