-- What the dashboard reads: only the newest benchmark run of each mode (quick and full).
with latest as (
    select distinct on (mode) benchmark_run_id
    from {{ ref('gold_scoreboard') }}
    order by mode, run_date desc, benchmark_run_id desc
)

select s.*
from {{ ref('gold_scoreboard') }} s
join latest l on l.benchmark_run_id = s.benchmark_run_id
