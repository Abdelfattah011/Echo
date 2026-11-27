-- One row per bug that ECHO found in a run (NOT a benchmark case: see silver_benchmark_results).
-- bug_type     = the detector label, e.g. 'Possible JOIN Fan-out'
-- bug_category = the standard name from bug_taxonomy.md (NULL when it is not one of the 5)
select
    case_id,
    run_id,
    step_id,
    suspicious_step_key,
    detector_name,
    trim(bug_type)             as bug_type,
    trim(bug_category)         as bug_category,
    upper(trim(severity))      as severity,
    lower(trim(confidence))    as confidence,
    coalesce(scope, 'main')    as scope,
    description,
    suggested_fix,
    fix_sql,
    (fix_sql is not null and trim(fix_sql) <> '') as has_fix,
    result_before,
    result_after,
    evidence,
    created_at
from {{ source('bronze', 'cases') }}
where case_id is not null
