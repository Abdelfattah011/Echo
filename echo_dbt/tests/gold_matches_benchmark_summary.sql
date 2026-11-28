{{ config(severity='warn') }}
-- Gold recomputes the numbers from the case rows. They must match the summary that
-- run_benchmark.py wrote for the same run (summary block of benchmark_results.json).
-- Warns (does not fail) when they differ by more than 0.05 points: then the loader or the benchmark is wrong.
select
    g.benchmark_run_id,
    g.detection_rate,       r.detection_rate       as summary_detection_rate,
    g.right_step_rate,      r.right_step_accuracy  as summary_right_step_accuracy,
    g.false_alarm_rate,     r.false_positive_rate  as summary_false_positive_rate
from {{ ref('gold_scoreboard') }} g
join {{ ref('silver_benchmark_runs') }} r on r.benchmark_run_id = g.benchmark_run_id
where g.is_overall
  and (   abs(g.detection_rate   - r.detection_rate)      > 0.05
       or abs(g.right_step_rate  - r.right_step_accuracy) > 0.05
       or abs(g.false_alarm_rate - r.false_positive_rate) > 0.05)
