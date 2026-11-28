-- The 'ALL' row must equal the sum of the bug-type rows. Returns rows only when it does not.
with per_type as (
    select
        benchmark_run_id,
        sum(broken_cases)    as broken_cases,
        sum(detected)        as detected,
        sum(right_step)      as right_step,
        sum(fixes_proposed)  as fixes_proposed,
        sum(fixes_passed)    as fixes_passed
    from {{ ref('gold_scoreboard') }}
    where not is_overall
    group by benchmark_run_id
),

overall as (
    select benchmark_run_id, broken_cases, detected, right_step, fixes_proposed, fixes_passed
    from {{ ref('gold_scoreboard') }}
    where is_overall
)

select o.benchmark_run_id
from overall o
join per_type t on t.benchmark_run_id = o.benchmark_run_id
where o.broken_cases   <> t.broken_cases
   or o.detected       <> t.detected
   or o.right_step     <> t.right_step
   or o.fixes_proposed <> t.fixes_proposed
   or o.fixes_passed   <> t.fixes_passed
