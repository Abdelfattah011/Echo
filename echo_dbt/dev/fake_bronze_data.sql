-- DEV ONLY: a few fake Bronze rows so the Silver models and tests have something to chew on.
-- Ids start at 900001 so they cannot collide with real identity values.
-- Run migrations 001, 002 and 003 first. Remove with the DELETE block at the bottom.

INSERT INTO bronze.runs (run_id, query_text, status, verdict) OVERRIDING SYSTEM VALUE VALUES
  (900001, 'SELECT o.order_id, a.address_line FROM orders o JOIN customer_addresses a ON o.customer_id = a.customer_id', 'DONE', 'issues_found'),
  (900002, 'SELECT order_id FROM orders WHERE amount > 100', 'DONE', 'no_issues_found');

INSERT INTO bronze.steps (step_id, run_id, step_number, step_type, step_key, scope, status, row_count, sql_snippet) OVERRIDING SYSTEM VALUE VALUES
  (900001, 900001, 1, 'BASE_TABLE', 'step_1', 'main', 'success', 3000, 'FROM orders o'),
  (900002, 900001, 2, 'JOIN',       'step_2', 'main', 'success', 9000, 'JOIN customer_addresses a ON o.customer_id = a.customer_id'),
  (900003, 900002, 1, 'BASE_TABLE', 'step_1', 'main', 'success', 3000, 'FROM orders'),
  (900004, 900002, 2, 'WHERE',      'step_2', 'main', 'success', 2100, 'WHERE amount > 100');

INSERT INTO bronze.cases (case_id, run_id, step_id, detector_name, bug_type, bug_category, severity, confidence, suspicious_step_key, description) OVERRIDING SYSTEM VALUE VALUES
  (900001, 900001, 900002, 'FanOutDetector', 'Possible JOIN Fan-out', 'JOIN Fan-out', 'High', 'high', 'step_2', 'Row count grew x3.00 at step_2');

INSERT INTO bronze.benchmark_runs
  (benchmark_run_id, mode, total_cases, broken_cases, clean_cases, detected_cases, failed_cases,
   detection_rate, right_step_accuracy, right_step_coverage, false_positives, false_positive_rate)
  OVERRIDING SYSTEM VALUE VALUES
  (900001, 'quick', 7, 5, 2, 5, 0, 100, 100, 100, 0, 0);

INSERT INTO bronze.benchmark_results
  (result_id, benchmark_run_id, case_id, bug_type, expected_step, is_clean, detected, right_step, false_positive)
  OVERRIDING SYSTEM VALUE VALUES
  (900001, 900001, 'FANOUT_001', 'JOIN Fan-out',              'step_2', false, true,  true,  false),
  (900002, 900001, 'TOTALS_001', 'Inflated Totals',           'step_2', false, true,  true,  false),
  (900003, 900001, 'LOST_001',   'Lost Rows in a LEFT JOIN',  'step_2', false, true,  true,  false),
  (900004, 900001, 'NULL_001',   'NULL Join Keys',            'step_2', false, true,  true,  false),
  (900005, 900001, 'TYPE_001',   'Key Type Mismatch',         'step_2', false, true,  true,  false),
  (900006, 900001, 'CLEAN_001',  'Clean',                      NULL,    true,  false, false, false),
  (900007, 900001, 'CLEAN_002',  'Clean',                      NULL,    true,  false, false, false);

-- CLEAN UP (run when you are done testing):
-- DELETE FROM bronze.benchmark_runs WHERE benchmark_run_id >= 900001;   -- cascades to benchmark_results
-- DELETE FROM bronze.runs           WHERE run_id >= 900001;             -- cascades to steps and cases
