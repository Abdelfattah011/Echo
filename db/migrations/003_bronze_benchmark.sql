-- =====================================================================
-- Migration 003: Benchmark tables in the Bronze layer
-- Run AFTER 001 and 002. Existing rows are kept. Safe to run more than once.
--
-- Source: M2's benchmark (run_benchmark.py -> benchmark_results.json)
--   benchmark_results.json = { mode, summary{...per_type}, results[ one per case ] }
--
-- Tables: bronze.benchmark_runs -> bronze.benchmark_results
--   * benchmark_runs    : one row per execution of the benchmark (full / quick)
--   * benchmark_results : one row per test query (case) inside that execution
--
-- Named "benchmark_*" on purpose: bronze.cases already means "a bug ECHO found".
-- A benchmark case is a TEST QUERY with a known answer, so it is a different thing.
--
-- Bronze is RAW: no CHECK constraints on values (same idea as 002).
-- Quality checks and the scoreboard (Gold) are done in dbt.
-- =====================================================================

-- 1. BENCHMARK_RUNS: one row per benchmark execution --------------------
CREATE TABLE IF NOT EXISTS bronze.benchmark_runs (
    benchmark_run_id            INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mode                        VARCHAR(20)  NOT NULL,       -- 'full' | 'quick'
    executed_at                 TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    git_commit                  VARCHAR(40),                 -- commit that was tested (set by CI, optional)
    is_baseline                 BOOLEAN      NOT NULL DEFAULT FALSE,  -- TRUE if this run was saved as baseline.json

    -- summary block of benchmark_results.json
    total_cases                 INT,
    broken_cases                INT,
    clean_cases                 INT,
    detected_cases              INT,
    failed_cases                INT,
    detection_rate              NUMERIC,                     -- %
    right_step_accuracy         NUMERIC,                     -- %
    right_step_coverage         NUMERIC,                     -- %
    false_positives             INT,
    false_positive_rate         NUMERIC,                     -- %
    average_execution_time_ms   NUMERIC,
    average_wall_clock_time_ms  NUMERIC,
    detector_errors             INT,
    per_type                    JSONB,                       -- summary.per_type, untouched
    raw_result                  JSONB                        -- the whole benchmark_results.json, untouched
);

-- 2. BENCHMARK_RESULTS: one row per test query --------------------------
CREATE TABLE IF NOT EXISTS bronze.benchmark_results (
    result_id               INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    benchmark_run_id        INT          NOT NULL
                                REFERENCES bronze.benchmark_runs(benchmark_run_id) ON DELETE CASCADE,
    case_id                 VARCHAR(50)  NOT NULL,           -- e.g. 'FANOUT_001', 'CLEAN_003'
    bug_type                VARCHAR(100) NOT NULL,           -- taxonomy name, or 'Clean'
    expected_step           VARCHAR(100),                    -- e.g. 'step_2' (NULL for clean queries)
    is_clean                BOOLEAN      NOT NULL,
    sql_text                TEXT,                            -- the test query (from generated_cases.json, optional)

    -- what ECHO did
    detected                BOOLEAN,
    right_step              BOOLEAN,
    false_positive          BOOLEAN,
    detected_bug_types      JSONB,                           -- list of detector labels
    suspicious_steps        JSONB,                           -- list of step keys

    -- speed
    execution_time_ms       NUMERIC,
    wall_clock_ms           NUMERIC,
    detector_times_ms       JSONB,                           -- {"FanOutDetector": 11.16, ...}

    -- problems
    detector_errors         INT,
    detector_error_details  JSONB,
    error                   TEXT,

    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_benchmark_results_run_case UNIQUE (benchmark_run_id, case_id)
);

-- 3. Indexes ------------------------------------------------------------
-- (benchmark_run_id is already covered by the unique constraint above)
CREATE INDEX IF NOT EXISTS idx_bronze_benchmark_runs_executed_at ON bronze.benchmark_runs(executed_at);
CREATE INDEX IF NOT EXISTS idx_bronze_benchmark_runs_mode        ON bronze.benchmark_runs(mode);
CREATE INDEX IF NOT EXISTS idx_bronze_benchmark_results_bug_type ON bronze.benchmark_results(bug_type);