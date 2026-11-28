-- One row per benchmark test query (case) per benchmark run.
-- The fix columns need migration 004.
-- outcome gives every case exactly one label, so Gold can count with simple filters:
--   ERROR                the case itself failed to run
--   FALSE_ALARM          clean query, but ECHO reported a bug
--   CLEAN_OK             clean query, ECHO stayed quiet
--   DETECTED_RIGHT_STEP  broken query, bug found at the correct step
--   DETECTED_WRONG_STEP  broken query, bug found but at another step
--   MISSED               broken query, no bug found
select
    result_id,
    benchmark_run_id,
    case_id,
    trim(bug_type)                     as bug_type,
    expected_step,
    is_clean,
    not is_clean                       as is_broken,
    coalesce(detected, false)          as detected,
    coalesce(right_step, false)        as right_step,
    coalesce(false_positive, false)    as false_positive,
    detected_bug_types,
    suspicious_steps,
    execution_time_ms,
    wall_clock_ms,
    detector_errors,
    error,
    coalesce(fix_proposed, false)      as fix_proposed,   -- NULL in Bronze = not measured = no fix
    lower(trim(fix_status))            as fix_status,
    fix_passed,                                           -- NULL = no fix proposed
    rows_before,
    rows_after,
    case
        when error is not null                                              then 'ERROR'
        when is_clean and coalesce(false_positive, false)                   then 'FALSE_ALARM'
        when is_clean                                                       then 'CLEAN_OK'
        when coalesce(detected, false) and coalesce(right_step, false)      then 'DETECTED_RIGHT_STEP'
        when coalesce(detected, false)                                      then 'DETECTED_WRONG_STEP'
        else 'MISSED'
    end                                as outcome,
    created_at
from {{ source('bronze', 'benchmark_results') }}
where result_id is not null
