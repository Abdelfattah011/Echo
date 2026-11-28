-- DEV ONLY: fake benchmark rows (with the fix columns) so Gold has something to calculate.
-- Run migrations 001, 002, 003 and 004 first.
-- Ids start at 900001 so they cannot collide with real identity values.

-- 0. Remove the fake benchmark rows from earlier (safe to run when there are none)
DELETE FROM bronze.benchmark_runs WHERE benchmark_run_id >= 900001;   -- cascades to benchmark_results

-- 1. Two quick runs. Run 900001 is imperfect on purpose, so the scoreboard has something to show.
INSERT INTO bronze.benchmark_runs
  (benchmark_run_id, mode, executed_at, total_cases, broken_cases, clean_cases, detected_cases, failed_cases,
   detection_rate, right_step_accuracy, right_step_coverage, false_positives, false_positive_rate)
  OVERRIDING SYSTEM VALUE VALUES
  (900001, 'quick', '2026-11-27 10:00+00', 7, 5, 2, 4, 0,  80.00,  75.00,  60.00, 1, 50.00),
  (900002, 'quick', '2026-11-28 10:00+00', 3, 2, 1, 2, 0, 100.00, 100.00, 100.00, 0,  0.00);

-- 2. Run 900001: one miss (TYPE_001), one wrong step (LOST_001), one false alarm (CLEAN_002)
INSERT INTO bronze.benchmark_results
  (result_id, benchmark_run_id, case_id, bug_type, expected_step, is_clean,
   detected, right_step, false_positive,
   fix_proposed, fix_status, fix_passed, rows_before, rows_after)
  OVERRIDING SYSTEM VALUE VALUES
  (900001, 900001, 'FANOUT_001', 'JOIN Fan-out',             'step_2', false, true,  true,  false, true,  'verified',  true,  9000, 3000),
  (900002, 900001, 'TOTALS_001', 'Inflated Totals',          'step_2', false, true,  true,  false, true,  'verified',  true,  9000, 3000),
  (900003, 900001, 'LOST_001',   'Lost Rows in a LEFT JOIN', 'step_2', false, true,  false, false, true,  'suggested', false,  500,  500),
  (900004, 900001, 'NULL_001',   'NULL Join Keys',           'step_2', false, true,  true,  false, false, 'manual',    NULL,  NULL, NULL),
  (900005, 900001, 'TYPE_001',   'Key Type Mismatch',        'step_2', false, false, false, false, false, 'none',      NULL,  NULL, NULL),
  (900006, 900001, 'CLEAN_001',  'Clean',                     NULL,    true,  false, false, false, false, 'none',      NULL,  NULL, NULL),
  (900007, 900001, 'CLEAN_002',  'Clean',                     NULL,    true,  false, false, true,  false, 'none',      NULL,  NULL, NULL);

-- 3. Run 900002 (newer): everything right, fix fields not filled yet (NULL), so fix_pass_rate must be NULL
INSERT INTO bronze.benchmark_results
  (result_id, benchmark_run_id, case_id, bug_type, expected_step, is_clean, detected, right_step, false_positive)
  OVERRIDING SYSTEM VALUE VALUES
  (900008, 900002, 'FANOUT_001', 'JOIN Fan-out',      'step_2', false, true,  true,  false),
  (900009, 900002, 'TYPE_001',   'Key Type Mismatch', 'step_2', false, true,  true,  false),
  (900010, 900002, 'CLEAN_001',  'Clean',              NULL,    true,  false, false, false);

-- Expected Gold row for run 900001, bug_type 'ALL':
--   total 7, broken 5, clean 2, detected 4 (detection_rate 80.00), right_step 3 (right_step_rate 75.00, coverage 60.00),
--   false_alarms 1 (false_alarm_rate 50.00), fixes_proposed 3, fixes_passed 2 (fix_pass_rate 66.67)
