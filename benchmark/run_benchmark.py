"""ECHO benchmark runner.

Runs the real engine (EchoEngineRunner) on queries whose answer is known (cases.py),
and measures how many bugs it catches and how often it complains about clean SQL.

    python benchmark/run_benchmark.py                    # full run, prints the table
    python benchmark/run_benchmark.py --quick            # small subset (used on pull requests)
    python benchmark/run_benchmark.py --quick --check    # CI gate: exit 1 if worse than baseline
    python benchmark/run_benchmark.py --update-baseline  # save today's numbers as the baseline

Outputs
    benchmark/results/latest.json   every number + one row per case (the Scoreboard page reads this)
    benchmark/baseline.json         the numbers a pull request must not drop below

Exit codes: 0 ok, 1 quality gate failed, 2 the benchmark itself could not run.
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import duckdb  # noqa: E402

from benchmark.cases import (  # noqa: E402
    ACCEPTED_LABELS,
    BUG_TITLES,
    CASES,
    INFORMATIONAL_LABELS,
    SETUP_SQL,
)
from Engine.runner.runner import EchoEngineRunner  # noqa: E402

DEFAULT_RESULTS = REPO_ROOT / "benchmark" / "results" / "latest.json"
DEFAULT_BASELINE = REPO_ROOT / "benchmark" / "baseline.json"

logging.getLogger("echo").setLevel(logging.ERROR)  # keep the table readable


# --------------------------------------------------------------------------- #
# Running one case
# --------------------------------------------------------------------------- #
def run_case(case):
    """Run ECHO on one case in a fresh in-memory database and score it."""
    row = {
        "id": case["id"],
        "bug": case["bug"],
        "sql": case["sql"],
        "found": [],
        "informational": [],
        "error": None,
        "ms": None,
    }
    conn = duckdb.connect(":memory:")
    started = time.perf_counter()
    try:
        conn.execute(SETUP_SQL)
        result = EchoEngineRunner(conn).analyze_sql(case["sql"])
        labels = [b.get("bug_type", "") for b in result.get("detected_bugs", [])]
        row["found"] = [x for x in labels if x not in INFORMATIONAL_LABELS]
        row["informational"] = [x for x in labels if x in INFORMATIONAL_LABELS]
        if result.get("verdict") == "error":
            row["error"] = "engine verdict was 'error'"
    except Exception as exc:  # noqa: BLE001 - a crash is a result, not a reason to stop
        row["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        row["ms"] = round((time.perf_counter() - started) * 1000, 1)
        conn.close()

    if case["bug"] == "clean":
        row["correct"] = not row["found"] and row["error"] is None
        row["outcome"] = "ok" if row["correct"] else "false_positive"
    else:
        accepted = ACCEPTED_LABELS[case["bug"]]
        row["correct"] = any(x in accepted for x in row["found"])
        row["outcome"] = "caught" if row["correct"] else "missed"
    return row


# --------------------------------------------------------------------------- #
# Numbers
# --------------------------------------------------------------------------- #
def rate(done, total):
    return round(done / total, 4) if total else None


def summarize(rows, mode):
    buggy = [r for r in rows if r["bug"] != "clean"]
    clean = [r for r in rows if r["bug"] == "clean"]
    per_type = {}
    for bug in BUG_TITLES:
        group = [r for r in buggy if r["bug"] == bug]
        if group:
            caught = sum(r["correct"] for r in group)
            per_type[bug] = {
                "title": BUG_TITLES[bug],
                "total": len(group),
                "caught": caught,
                "detection_rate": rate(caught, len(group)),
                "missed": [r["id"] for r in group if not r["correct"]],
            }
    caught_all = sum(r["correct"] for r in buggy)
    fps = [r["id"] for r in clean if not r["correct"]]
    times = [r["ms"] for r in rows if r["ms"] is not None]
    return {
        "mode": mode,
        "total_cases": len(rows),
        "buggy_cases": len(buggy),
        "clean_cases": len(clean),
        "overall_detection_rate": rate(caught_all, len(buggy)),
        "false_positives": len(fps),
        "false_positive_ids": fps,
        "false_positive_rate": rate(len(fps), len(clean)),
        "avg_ms_per_case": round(sum(times) / len(times), 1) if times else None,
        "per_type": per_type,
    }


# --------------------------------------------------------------------------- #
# Quality gate
# --------------------------------------------------------------------------- #
def check_against_baseline(summary, baseline_path, tolerance):
    """Return (ok, problems). Fails if detection dropped or false positives grew."""
    if not baseline_path.exists():
        return False, [f"No baseline file at {baseline_path}. Run with --update-baseline first."]
    saved = json.loads(baseline_path.read_text(encoding="utf-8")).get(summary["mode"])
    if saved is None:
        return False, [f"Baseline has no '{summary['mode']}' entry. Run with --update-baseline."]

    problems = []
    if summary["overall_detection_rate"] < saved["overall_detection_rate"] - tolerance:
        problems.append(
            f"overall detection rate fell: {saved['overall_detection_rate']:.1%} -> "
            f"{summary['overall_detection_rate']:.1%}"
        )
    for bug, base in saved.get("per_type", {}).items():
        now = summary["per_type"].get(bug)
        if now is None:
            continue
        if now["detection_rate"] < base["detection_rate"] - tolerance:
            problems.append(
                f"{base['title']}: {base['detection_rate']:.0%} -> {now['detection_rate']:.0%}"
                f" (missed now: {', '.join(now['missed'])})"
            )
    if summary["false_positives"] > saved["false_positives"]:
        problems.append(
            f"false positives grew: {saved['false_positives']} -> {summary['false_positives']}"
            f" ({', '.join(summary['false_positive_ids'])})"
        )
    return not problems, problems


def save_baseline(summary, baseline_path):
    data = {}
    if baseline_path.exists():
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
    keep = ("total_cases", "overall_detection_rate", "false_positives", "per_type")
    data[summary["mode"]] = {k: summary[k] for k in keep}
    baseline_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def print_report(summary, rows, verbose):
    print(f"\nECHO benchmark ({summary['mode']}): {summary['total_cases']} cases")
    print("-" * 64)
    print(f"{'Bug type':<28}{'Caught':>8}{'Rate':>8}   Missed")
    for item in summary["per_type"].values():
        print(
            f"{item['title']:<28}{item['caught']:>3}/{item['total']:<4}"
            f"{item['detection_rate']:>8.0%}   {', '.join(item['missed']) or '-'}"
        )
    print("-" * 64)
    print(f"Overall detection rate : {summary['overall_detection_rate']:.1%}")
    print(
        f"False positives        : {summary['false_positives']}/{summary['clean_cases']} clean queries"
        + (f"  ({', '.join(summary['false_positive_ids'])})" if summary["false_positive_ids"] else "")
    )
    print(f"Average time per case  : {summary['avg_ms_per_case']} ms")
    if verbose:
        print("\nPer case:")
        for r in rows:
            print(f"  {r['id']:<13}{r['outcome']:<16}found={r['found']}" + (f" ERROR={r['error']}" if r["error"] else ""))


def write_markdown_summary(summary, ok, problems):
    """On GitHub Actions this shows up on the run page."""
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return
    lines = [
        f"### ECHO benchmark ({summary['mode']}) - {'PASS' if ok else 'BLOCKED'}",
        "",
        "| Bug type | Caught | Rate |",
        "|---|---|---|",
    ]
    for item in summary["per_type"].values():
        lines.append(f"| {item['title']} | {item['caught']}/{item['total']} | {item['detection_rate']:.0%} |")
    lines += [
        "",
        f"Overall detection: **{summary['overall_detection_rate']:.1%}** - "
        f"false positives: **{summary['false_positives']}/{summary['clean_cases']}**",
    ]
    if problems:
        lines += ["", "**Why it was blocked:**"] + [f"- {p}" for p in problems]
    with open(target, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the ECHO detection benchmark.")
    parser.add_argument("--quick", action="store_true", help="only the small subset (for pull requests)")
    parser.add_argument("--check", action="store_true", help="compare with the baseline; exit 1 if worse")
    parser.add_argument("--update-baseline", action="store_true", help="save this run as the new baseline")
    parser.add_argument("--tolerance", type=float, default=0.0, help="allowed drop in detection rate, e.g. 0.05")
    parser.add_argument("--output", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--verbose", action="store_true", help="print one line per case")
    args = parser.parse_args(argv)

    mode = "quick" if args.quick else "full"
    cases = [c for c in CASES if c.get("quick")] if args.quick else CASES
    try:
        rows = [run_case(c) for c in cases]
    except Exception as exc:  # noqa: BLE001
        print(f"Benchmark could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    crashed = [r["id"] for r in rows if r["error"] and r["error"] != "engine verdict was 'error'"]
    summary = summarize(rows, mode)
    print_report(summary, rows, args.verbose)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"summary": summary, "cases": rows}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"\nResults written to {args.output}")

    if crashed:
        print(f"Cases that crashed instead of returning a result: {', '.join(crashed)}", file=sys.stderr)
        return 2

    if args.update_baseline:
        save_baseline(summary, args.baseline)
        print(f"Baseline ({mode}) saved to {args.baseline}")

    if args.check:
        ok, problems = check_against_baseline(summary, args.baseline, args.tolerance)
        write_markdown_summary(summary, ok, problems)
        if ok:
            print("\nQuality gate: PASS")
            return 0
        print("\nQuality gate: BLOCKED")
        for p in problems:
            print(f"  - {p}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())