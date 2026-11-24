
"""ECHO benchmark runner.

Runs generated SQL benchmark cases against the real ECHO engine.

Metrics:
- Detection Rate
- Right Step Accuracy
- Right Step Coverage
- False Positives / False Positive Rate
- Average Engine Execution Time
- Average Wall-clock Time
- Detector Errors

Examples:
    python -m benchmark.run_benchmark
    python -m benchmark.run_benchmark --quick
    python -m benchmark.run_benchmark --check
    python -m benchmark.run_benchmark --quick --check
    python -m benchmark.run_benchmark --update-baseline
"""

import argparse
import json
import logging
import os
import time

from collections import defaultdict
from pathlib import Path

import duckdb

from Engine.runner.runner import EchoEngineRunner


# ---------------------------------------------------------
# Paths and configuration
# ---------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_DIR = PROJECT_ROOT / "benchmark"

CASES_FILE = BENCHMARK_DIR / "generated_cases.json"
DATABASE_FILE = PROJECT_ROOT / "echo_benchmark.duckdb"

RESULTS_FILE = BENCHMARK_DIR / "benchmark_results.json"
BASELINE_FILE = BENCHMARK_DIR / "baseline.json"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger("echo.benchmark")


# ---------------------------------------------------------
# Expected detector labels
# ---------------------------------------------------------

ACCEPTED_BUG_TYPES = {
    "JOIN Fan-out": [
        "Possible JOIN Fan-out",
    ],
    "Inflated Totals": [
        "Inflated Aggregate",
    ],
    "Lost Rows in a LEFT JOIN": [
        "Lost Rows in a LEFT JOIN",
    ],
    "NULL Join Keys": [
        "NULL Join Keys",
    ],
    "Key Type Mismatch": [
        "Join Key Type Mismatch",
    ],
}


# ---------------------------------------------------------
# Load and validate benchmark cases
# ---------------------------------------------------------

def load_cases():
    if not CASES_FILE.exists():
        raise FileNotFoundError(
            f"Benchmark cases file not found: {CASES_FILE}"
        )

    with CASES_FILE.open("r", encoding="utf-8") as file:
        cases = json.load(file)

    if not isinstance(cases, list) or not cases:
        raise ValueError(
            "Benchmark cases file is empty or invalid."
        )

    required_fields = {
        "case_id",
        "bug_type",
        "sql",
    }

    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            raise ValueError(
                f"Case #{index} must be a JSON object."
            )

        missing = required_fields - case.keys()

        if missing:
            raise ValueError(
                f"Case #{index} is missing fields: {sorted(missing)}"
            )

        if not case.get("is_clean", False):
            bug_type = case["bug_type"]

            if bug_type not in ACCEPTED_BUG_TYPES:
                raise ValueError(
                    f"Unknown bug type in case {case['case_id']}: "
                    f"{bug_type}"
                )

    return cases


# ---------------------------------------------------------
# Run one benchmark case
# ---------------------------------------------------------

def run_case(case):
    case_id = case["case_id"]
    bug_type = case["bug_type"]
    sql = case["sql"]

    expected_step = case.get("expected_step")
    is_clean = case.get("is_clean", False)

    result = {
        "case_id": case_id,
        "bug_type": bug_type,
        "expected_step": expected_step,
        "is_clean": is_clean,

        "detected": False,
        "right_step": False,
        "false_positive": False,

        "detected_bug_types": [],
        "suspicious_steps": [],

        "execution_time_ms": 0.0,
        "wall_clock_ms": 0.0,
        "detector_times_ms": {},

        "detector_errors": 0,
        "detector_error_details": [],

        "error": None,
    }

    connection = None
    started = time.perf_counter()

    try:
        connection = duckdb.connect(
            str(DATABASE_FILE),
            read_only=True,
        )

        engine = EchoEngineRunner(
            connection,
            collect_column_metrics=False,
        )

        analysis = engine.analyze_sql(sql)

        detected_bugs = analysis.get("detected_bugs", [])

        result["detected_bug_types"] = [
            bug.get("bug_type")
            for bug in detected_bugs
        ]

        result["suspicious_steps"] = [
            bug.get("suspicious_step_id")
            for bug in detected_bugs
        ]

        profiling = analysis.get("profiling", {})

        result["execution_time_ms"] = profiling.get(
            "total_ms", 0.0
        )

        result["detector_times_ms"] = profiling.get(
            "detectors_ms", {}
        )

        detector_errors = analysis.get("detector_errors", [])

        result["detector_errors"] = len(detector_errors)
        result["detector_error_details"] = detector_errors

        if is_clean:
            # Any reported bug in a clean case is a false positive.
            result["false_positive"] = bool(detected_bugs)

        else:
            accepted_labels = ACCEPTED_BUG_TYPES[bug_type]

            # Only findings matching the expected bug type count.
            matching_bugs = [
                bug
                for bug in detected_bugs
                if bug.get("bug_type") in accepted_labels
            ]

            result["detected"] = bool(matching_bugs)

            # Check the step of the matching detector finding only.
            result["right_step"] = any(
                bug.get("suspicious_step_id") == expected_step
                for bug in matching_bugs
            )

    except Exception as exc:
        result["error"] = repr(exc)
        logger.exception(
            "Failed benchmark case %s",
            case_id,
        )

    finally:
        result["wall_clock_ms"] = round(
            (time.perf_counter() - started) * 1000,
            2,
        )

        if connection is not None:
            connection.close()

    return result


# ---------------------------------------------------------
# Quick mode
# ---------------------------------------------------------

def select_quick_cases(cases):
    """
    Select one case per bug type and two clean cases.
    Keeps quick mode compatible with the original 7-case baseline.
    """
    selected = []
    counts = defaultdict(int)

    for case in cases:
        category = (
            "Clean"
            if case.get("is_clean", False)
            else case["bug_type"]
        )

        limit = 2 if category == "Clean" else 1

        if counts[category] < limit:
            selected.append(case)
            counts[category] += 1

    return selected


# ---------------------------------------------------------
# Calculate benchmark metrics
# ---------------------------------------------------------

def calculate_summary(results):
    broken_cases = [
        item for item in results
        if not item["is_clean"]
    ]

    clean_cases = [
        item for item in results
        if item["is_clean"]
    ]

    detected_cases = [
        item for item in broken_cases
        if item["detected"]
    ]

    right_step_cases = [
        item for item in detected_cases
        if item["right_step"]
    ]

    false_positives = [
        item for item in clean_cases
        if item["false_positive"]
    ]

    failed_cases = [
        item for item in results
        if item["error"] is not None
    ]

    total_broken = len(broken_cases)
    total_clean = len(clean_cases)
    total_detected = len(detected_cases)

    detection_rate = (
        total_detected / total_broken * 100
        if total_broken else 0.0
    )

    right_step_accuracy = (
        len(right_step_cases) / total_detected * 100
        if total_detected else 0.0
    )

    right_step_coverage = (
        len(right_step_cases) / total_broken * 100
        if total_broken else 0.0
    )

    false_positive_rate = (
        len(false_positives) / total_clean * 100
        if total_clean else 0.0
    )

    average_execution_time = (
        sum(item["execution_time_ms"] for item in results)
        / len(results)
        if results else 0.0
    )

    average_wall_clock_time = (
        sum(item["wall_clock_ms"] for item in results)
        / len(results)
        if results else 0.0
    )

    detector_errors = sum(
        item["detector_errors"]
        for item in results
    )

    per_type = {}

    for bug_type in ACCEPTED_BUG_TYPES:
        category_cases = [
            item for item in broken_cases
            if item["bug_type"] == bug_type
        ]

        category_detected = [
            item for item in category_cases
            if item["detected"]
        ]

        category_right = [
            item for item in category_detected
            if item["right_step"]
        ]

        total = len(category_cases)

        per_type[bug_type] = {
            "total": total,
            "detected": len(category_detected),

            "detection_rate": round(
                len(category_detected) / total * 100,
                2,
            ) if total else 0.0,

            "right_step": len(category_right),

            "right_step_accuracy": round(
                len(category_right) / len(category_detected) * 100,
                2,
            ) if category_detected else 0.0,

            "right_step_coverage": round(
                len(category_right) / total * 100,
                2,
            ) if total else 0.0,
        }

    return {
        "total_cases": len(results),
        "broken_cases": total_broken,
        "clean_cases": total_clean,

        "detected_cases": total_detected,
        "failed_cases": len(failed_cases),

        "detection_rate": round(detection_rate, 2),
        "right_step_accuracy": round(right_step_accuracy, 2),
        "right_step_coverage": round(right_step_coverage, 2),

        "false_positives": len(false_positives),
        "false_positive_rate": round(false_positive_rate, 2),

        "average_execution_time_ms": round(
            average_execution_time, 2
        ),

        "average_wall_clock_time_ms": round(
            average_wall_clock_time, 2
        ),

        "detector_errors": detector_errors,
        "per_type": per_type,
    }


# ---------------------------------------------------------
# Print results
# ---------------------------------------------------------

def print_summary(summary):
    print("\n" + "=" * 68)
    print("ECHO BENCHMARK RESULTS")
    print("=" * 68)

    print(f"Total cases:             {summary['total_cases']}")
    print(f"Broken cases:            {summary['broken_cases']}")
    print(f"Clean cases:             {summary['clean_cases']}")
    print(f"Failed cases:            {summary['failed_cases']}")

    print("-" * 68)

    print(f"Detection Rate:          {summary['detection_rate']}%")
    print(f"Right Step Accuracy:     {summary['right_step_accuracy']}%")
    print(f"Right Step Coverage:     {summary['right_step_coverage']}%")
    print(f"False Positives:         {summary['false_positives']}")
    print(f"False Positive Rate:     {summary['false_positive_rate']}%")

    print("-" * 68)

    print(
        "Average Engine Time:    "
        f"{summary['average_execution_time_ms']} ms"
    )

    print(
        "Average Wall-clock:     "
        f"{summary['average_wall_clock_time_ms']} ms"
    )

    print(f"Detector Errors:         {summary['detector_errors']}")

    print("-" * 68)
    print("RESULTS BY BUG TYPE")
    print("-" * 68)

    for bug_type, metrics in summary["per_type"].items():
        print(f"\n{bug_type}")

        print(
            f"  Detected: {metrics['detected']}/{metrics['total']}"
        )

        print(
            f"  Detection Rate: {metrics['detection_rate']:.2f}%"
        )

        print(
            f"  Right Step Accuracy: "
            f"{metrics['right_step_accuracy']:.2f}%"
        )

        print(
            f"  Right Step Coverage: "
            f"{metrics['right_step_coverage']:.2f}%"
        )

    print("=" * 68)

# ---------------------------------------------------------
# Verbose case report
# ---------------------------------------------------------

def print_verbose_results(results):
    print("\n" + "=" * 68)
    print("BENCHMARK CASE DETAILS")
    print("=" * 68)

    for item in results:
        status = (
            "FALSE POSITIVE"
            if item["false_positive"]
            else "DETECTED"
            if item["detected"]
            else "NOT DETECTED"
            if not item["is_clean"]
            else "CLEAN"
        )

        print(f"\nCase: {item['case_id']}")
        print(f"Bug Type: {item['bug_type']}")
        print(f"Status: {status}")
        print(f"Expected Step: {item['expected_step']}")
        print(f"Right Step: {item['right_step']}")
        print(f"Detected Labels: {item['detected_bug_types']}")
        print(f"Suspicious Steps: {item['suspicious_steps']}")
        print(f"Execution Time: {item['execution_time_ms']} ms")
        print(f"Detector Errors: {item['detector_errors']}")

        if item["error"]:
            print(f"ERROR: {item['error']}")

# ---------------------------------------------------------
# Save benchmark results
# ---------------------------------------------------------

def save_results(results, summary, output_file, mode):
    output_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    output = {
        "mode": mode,
        "summary": summary,
        "results": results,
    }

    with output_file.open("w", encoding="utf-8") as file:
        json.dump(
            output,
            file,
            indent=4,
            ensure_ascii=False,
        )

    logger.info(
        "Benchmark results saved to: %s",
        output_file,
    )
# ---------------------------------------------------------
# Baseline management
# ---------------------------------------------------------
BASELINE_TYPE_KEYS = {
    "JOIN Fan-out": "join_fanout",
    "Inflated Totals": "inflated_totals",
    "Lost Rows in a LEFT JOIN": "left_join_row_loss",
    "NULL Join Keys": "null_join_keys",
    "Key Type Mismatch": "key_type_mismatch",
}
def load_baseline(baseline_file):
    if not baseline_file.exists():
        raise FileNotFoundError(
            f"Baseline file not found: {baseline_file}"
        )

    with baseline_file.open("r", encoding="utf-8") as file:
        baseline = json.load(file)

    if not isinstance(baseline, dict):
        raise ValueError("Invalid baseline format.")

    return baseline

def normalize_baseline(baseline_data, mode):
    """
    Supports both:
    1. Original baseline format:
       {
           "full": {
               "total_cases": 21,
               "overall_detection_rate": 0.6,
               ...
           }
       }
    2. New baseline format:
       {
           "full": {
               "summary": {
                   "detection_rate": 60.0,
                   ...
               }
           }
       }
    """

    if mode not in baseline_data:
        raise ValueError(
            f"No '{mode}' baseline found."
        )

    mode_data = baseline_data[mode]

    if not isinstance(mode_data, dict):
        raise ValueError(
            f"Invalid '{mode}' baseline format."
        )

    # New format
    if isinstance(mode_data.get("summary"), dict):
        return mode_data["summary"]

    # Original format
    if "overall_detection_rate" not in mode_data:
        raise ValueError(
            f"Invalid '{mode}' baseline: "
            "missing overall_detection_rate or summary."
        )

    old_per_type = mode_data.get("per_type", {})
    normalized_per_type = {}
    for bug_type, old_key in BASELINE_TYPE_KEYS.items():
        old_metrics = old_per_type.get(old_key)
        if not isinstance(old_metrics, dict):
            continue

        old_rate = old_metrics.get("detection_rate", 0.0)
        normalized_per_type[bug_type] = {
            "total": old_metrics.get("total", 0),
            "detected": old_metrics.get("caught", 0),
            "detection_rate": round(old_rate * 100, 2),
        }

    total_cases = mode_data.get("total_cases", 0)
    total_broken = sum(
        metrics.get("total", 0)
        for metrics in normalized_per_type.values()
    )

    # Infer clean cases from total cases when not explicitly stored.
    total_clean = max(0, total_cases - total_broken)
    false_positives = mode_data.get("false_positives", 0)
    false_positive_rate = (
        false_positives / total_clean * 100
        if total_clean else 0.0
    )

    return {
        "total_cases": total_cases,
        "broken_cases": total_broken,
        "clean_cases": total_clean,

        "detection_rate": round(
            mode_data["overall_detection_rate"] * 100,
            2,
        ),

        "false_positives": false_positives,
        "false_positive_rate": round(
            false_positive_rate,
            2,
        ),

        "per_type": normalized_per_type,
    }

def save_baseline(summary, baseline_file, mode):
    """
    Update only the selected mode.
    Preserve the other mode and existing baseline data.
    """

    baseline_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    baseline = {}
    if baseline_file.exists():
        with baseline_file.open("r", encoding="utf-8") as file:
            baseline = json.load(file)

        if not isinstance(baseline, dict):
            raise ValueError("Invalid existing baseline format.")

    # Keep the new summary under the selected mode.
    # Other modes, including the original legacy mode, are preserved.
    baseline[mode] = {
        "summary": summary,
    }

    with baseline_file.open("w", encoding="utf-8") as file:
        json.dump(
            baseline,
            file,
            indent=4,
            ensure_ascii=False,
        )
    logger.info(
        "%s baseline updated: %s",
        mode.capitalize(),
        baseline_file,
    )

# ---------------------------------------------------------
# Quality gate
# ---------------------------------------------------------

def check_against_baseline(
    summary,
    baseline_file,
    mode,
    tolerance=0.0,
):
    baseline_data = load_baseline(baseline_file)

    baseline_summary = normalize_baseline(
        baseline_data,
        mode,
    )

    failures = []

    # -----------------------------------------------------
    # Overall detection rate
    # -----------------------------------------------------
    baseline_detection = baseline_summary.get(
        "detection_rate",
        0.0,
    )
    current_detection = summary["detection_rate"]
    if current_detection < baseline_detection - tolerance:
        failures.append(
            "Detection rate decreased: "
            f"{baseline_detection}% -> {current_detection}%"
        )
    # -----------------------------------------------------
    # False positive rate
    # -----------------------------------------------------
    baseline_fp_rate = baseline_summary.get(
        "false_positive_rate",
        0.0,
    )

    current_fp_rate = summary["false_positive_rate"]

    if current_fp_rate > baseline_fp_rate + tolerance:
        failures.append(
            "False positive rate increased: "
            f"{baseline_fp_rate}% -> {current_fp_rate}%"
        )
    # -----------------------------------------------------
    # Per-type detection rate
    # -----------------------------------------------------
    baseline_per_type = baseline_summary.get(
        "per_type",
        {},
    )
    current_per_type = summary["per_type"]
    for bug_type in ACCEPTED_BUG_TYPES:

        if bug_type not in baseline_per_type:
            failures.append(
                f"Missing '{bug_type}' in baseline."
            )
            continue

        if bug_type not in current_per_type:
            failures.append(
                f"Missing '{bug_type}' in current results."
            )
            continue

        old_rate = baseline_per_type[bug_type].get(
            "detection_rate",
            0.0,
        )
        new_rate = current_per_type[bug_type].get(
            "detection_rate",
            0.0,
        )

        if new_rate < old_rate - tolerance:
            failures.append(
                f"{bug_type} detection rate decreased: "
                f"{old_rate}% -> {new_rate}%"
            )
    # -----------------------------------------------------
    # Result
    # -----------------------------------------------------
    if failures:
        print("\nQUALITY GATE: FAILED")

        for failure in failures:
            print(f"  - {failure}")
        return False
    print("\nQUALITY GATE: PASSED")
    print("Current benchmark is within the baseline tolerance.")

    return True


# ---------------------------------------------------------
# GitHub Actions Summary
# ---------------------------------------------------------

def write_github_summary(summary, mode, output_file):
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")

    if not summary_path:
        return

    lines = [
        "# ECHO Benchmark Results",
        "",
        f"**Mode:** {mode}",
        "",
        "## Overall Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Total Cases | {summary['total_cases']} |",
        f"| Broken Cases | {summary['broken_cases']} |",
        f"| Clean Cases | {summary['clean_cases']} |",
        f"| Failed Cases | {summary['failed_cases']} |",
        f"| Detection Rate | {summary['detection_rate']}% |",
        f"| Right Step Accuracy | {summary['right_step_accuracy']}% |",
        f"| Right Step Coverage | {summary['right_step_coverage']}% |",
        f"| False Positives | {summary['false_positives']} |",
        f"| False Positive Rate | {summary['false_positive_rate']}% |",
        f"| Average Engine Time | {summary['average_execution_time_ms']} ms |",
        f"| Average Wall-clock Time | {summary['average_wall_clock_time_ms']} ms |",
        f"| Detector Errors | {summary['detector_errors']} |",
        "",
        "## Results by Bug Type",
        "",
        "| Bug Type | Detected | Detection Rate | Right Step Accuracy |",
        "|---|---:|---:|---:|",
    ]

    for bug_type, metrics in summary["per_type"].items():
        lines.append(
            f"| {bug_type} "
            f"| {metrics['detected']}/{metrics['total']} "
            f"| {metrics['detection_rate']}% "
            f"| {metrics['right_step_accuracy']}% |"
        )

    lines.extend([
        "",
        f"**JSON Results:** `{output_file}`",
        "",
    ])

    with open(summary_path, "a", encoding="utf-8") as file:
        file.write("\n".join(lines))


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Run the ECHO SQL benchmark."
    )

    parser.add_argument(
        "--quick",
        action="store_true",
        help="Run a small benchmark subset.",
    )

    parser.add_argument(
        "--check",
        action="store_true",
        help="Compare benchmark results against the saved baseline.",
    )

    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Save current results as the baseline for this mode.",
    )

    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.0,
        help="Allowed metric decrease/increase in percentage points.",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=RESULTS_FILE,
        help="Path for the benchmark results JSON file.",
    )

    parser.add_argument(
        "--baseline",
        type=Path,
        default=BASELINE_FILE,
        help="Path for the benchmark baseline JSON file.",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print details for every benchmark case.",
    )

    args = parser.parse_args()

    if args.check and args.update_baseline:
        parser.error(
            "--check and --update-baseline cannot be used together."
        )

    if args.tolerance < 0:
        parser.error("--tolerance cannot be negative.")

    mode = "quick" if args.quick else "full"

    logger.info("Starting ECHO benchmark in %s mode...", mode)

    if not DATABASE_FILE.exists():
        raise FileNotFoundError(
            f"Database not found: {DATABASE_FILE}\n"
            "Run: python benchmark/data_generator.py"
        )

    cases = load_cases()

    if args.quick:
        cases = select_quick_cases(cases)

        logger.info(
            "Quick mode enabled: %s cases selected.",
            len(cases),
        )

    logger.info(
        "Loaded %s benchmark cases.",
        len(cases),
    )

    results = []

    for index, case in enumerate(cases, start=1):
        logger.info(
            "Running case %s/%s: %s",
            index,
            len(cases),
            case["case_id"],
        )

        result = run_case(case)
        results.append(result)

    summary = calculate_summary(results)

    print_summary(summary)

    if args.verbose:
        print_verbose_results(results)

    save_results(
        results,
        summary,
        args.output,
        mode,
    )

    write_github_summary(
        summary,
        mode,
        args.output,
    )

    if args.update_baseline:
        save_baseline(
            summary,
            args.baseline,
            mode,
        )

    quality_gate_passed = True

    if args.check:
        quality_gate_passed = check_against_baseline(
            summary,
            args.baseline,
            mode,
            args.tolerance,
        )

    logger.info("Benchmark completed.")

    if summary["failed_cases"] > 0:
        logger.warning(
            "%s benchmark cases failed during execution.",
            summary["failed_cases"],
        )

    if args.check and not quality_gate_passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()