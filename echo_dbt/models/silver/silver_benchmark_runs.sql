-- One row per execution of the benchmark (full or quick).
-- per_type and raw_result stay in Bronze: Gold recomputes the numbers from the case rows.
select
    benchmark_run_id,
    lower(trim(mode))          as mode,
    executed_at,
    git_commit,
    is_baseline,
    total_cases,
    broken_cases,
    clean_cases,
    detected_cases,
    failed_cases,
    detection_rate,
    right_step_accuracy,
    right_step_coverage,
    false_positives,
    false_positive_rate,
    average_execution_time_ms,
    average_wall_clock_time_ms,
    detector_errors
from {{ source('bronze', 'benchmark_runs') }}
where benchmark_run_id is not null
