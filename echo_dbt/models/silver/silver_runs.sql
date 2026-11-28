-- One row per analysis run. Cleaning only: trim and normalise text, drop rows without a key.
-- raw_result (the whole engine output) is left in Bronze on purpose: it is large.
select
    run_id,
    query_text,
    executed_at,
    upper(trim(status))   as status,
    lower(trim(verdict))  as verdict
from {{ source('bronze', 'runs') }}
where run_id is not null
