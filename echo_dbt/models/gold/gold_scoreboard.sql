-- The scoreboard: how well ECHO finds each type of known bug.
-- One row per benchmark run per bug type, plus one 'ALL' row per run (all cases together).
--
-- Definitions (same as run_benchmark.py; all rates are percentages, 0 to 100):
--   detection_rate      = detected broken cases            / broken cases
--   right_step_rate     = broken cases at the right step   / detected broken cases
--   right_step_coverage = broken cases at the right step   / broken cases
--   false_alarms        = clean cases where ECHO reported a bug (count); false_alarm_rate = / clean cases
--   fix_pass_rate       = fixes whose before/after check passed / fixes proposed
-- A rate is NULL when its denominator is 0 (never 0, never an error).
-- A case that failed to run (error) stays in the denominators, so it counts as a miss.
-- Clean cases belong to no bug type, so false alarms appear only on the 'ALL' row.

with results as (
    select * from {{ ref('silver_benchmark_results') }}
),

runs as (
    select
        benchmark_run_id,
        mode,
        executed_at as run_date,
        is_baseline,
        git_commit
    from {{ ref('silver_benchmark_runs') }}
),

-- every broken case under its own bug type, and every case once more under 'ALL'
scoped as (
    select
        benchmark_run_id, bug_type as scope_name,
        is_clean, is_broken, detected, right_step, false_positive,
        fix_proposed, fix_passed, outcome
    from results
    where is_broken

    union all

    select
        benchmark_run_id, 'ALL' as scope_name,
        is_clean, is_broken, detected, right_step, false_positive,
        fix_proposed, fix_passed, outcome
    from results
),

counts as (
    select
        benchmark_run_id,
        scope_name,
        count(*)                                                                   as n_total,
        count(*) filter (where is_broken)                                          as n_broken,
        count(*) filter (where is_clean)                                           as n_clean,
        count(*) filter (where is_broken and detected)                             as n_detected,
        count(*) filter (where is_broken and detected and right_step)              as n_right_step,
        count(*) filter (where is_clean and false_positive)                        as n_false_alarms,
        count(*) filter (where outcome = 'ERROR')                                  as n_errors,
        count(*) filter (where is_broken and fix_proposed)                         as n_fixes_proposed,
        count(*) filter (where is_broken and fix_proposed and coalesce(fix_passed, false)) as n_fixes_passed
    from scoped
    group by benchmark_run_id, scope_name
)

select
    c.benchmark_run_id,
    r.mode,
    r.run_date,
    r.is_baseline,
    r.git_commit,
    c.scope_name                                                    as bug_type,
    (c.scope_name = 'ALL')                                          as is_overall,

    c.n_total                                                       as total_cases,
    c.n_broken                                                      as broken_cases,
    c.n_clean                                                       as clean_cases,

    c.n_detected                                                    as detected,
    round(100.0 * c.n_detected   / nullif(c.n_broken, 0), 2)        as detection_rate,

    c.n_right_step                                                  as right_step,
    round(100.0 * c.n_right_step / nullif(c.n_detected, 0), 2)      as right_step_rate,
    round(100.0 * c.n_right_step / nullif(c.n_broken, 0), 2)        as right_step_coverage,

    c.n_false_alarms                                                as false_alarms,
    round(100.0 * c.n_false_alarms / nullif(c.n_clean, 0), 2)       as false_alarm_rate,

    c.n_fixes_proposed                                              as fixes_proposed,
    c.n_fixes_passed                                                as fixes_passed,
    round(100.0 * c.n_fixes_passed / nullif(c.n_fixes_proposed, 0), 2) as fix_pass_rate,

    c.n_errors                                                      as error_cases
from counts c
join runs r on r.benchmark_run_id = c.benchmark_run_id
