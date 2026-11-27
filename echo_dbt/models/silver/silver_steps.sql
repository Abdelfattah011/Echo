-- One row per parsed step of a run.
-- prev_row_count = row_count of the previous step in the same scope (main, a CTE name ...).
-- It is NULL for the first step of a scope and for steps that have no row_count.
select
    step_id,
    run_id,
    step_number,
    step_key,
    upper(trim(step_type))     as step_type,
    step_name,
    coalesce(scope, 'main')    as scope,
    lower(trim(status))        as status,
    row_count,
    lag(row_count) over (
        partition by run_id, coalesce(scope, 'main')
        order by step_number
    )                          as prev_row_count,
    sum_value,
    execution_time_ms,
    error,
    created_at
from {{ source('bronze', 'steps') }}
where step_id is not null
