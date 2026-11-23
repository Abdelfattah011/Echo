-- =====================================================================
-- Migration 001: Bronze Layer Schema for ECHO (PostgreSQL)
-- Tables: bronze.runs -> bronze.steps, bronze.cases
-- Every step and every case belongs to exactly one run.
-- A case can also point to the step where its bug appeared (step_id).
-- =====================================================================

-- 1. Dedicated schema for the Bronze layer
CREATE SCHEMA IF NOT EXISTS bronze;

-- 2. RUNS: one row per analysis session of one SQL query
CREATE TABLE IF NOT EXISTS bronze.runs (
    run_id       INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    query_text   TEXT         NOT NULL,                     -- the SQL query the user submitted
    executed_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    status       VARCHAR(50)  NOT NULL DEFAULT 'PENDING',
    CONSTRAINT chk_runs_status
        CHECK (status IN ('PENDING', 'RUNNING', 'DONE', 'FAILED'))
);

-- 3. STEPS: each parsed step of the query, with what it produced
--    (written by the parser / step runner)
CREATE TABLE IF NOT EXISTS bronze.steps (
    step_id      INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id       INT          NOT NULL
                     REFERENCES bronze.runs(run_id) ON DELETE CASCADE,
    step_number  INT          NOT NULL,                     -- order inside the run (1, 2, 3...)
    step_type    VARCHAR(50)  NOT NULL,
    step_name    VARCHAR(100),
    sql_snippet  TEXT         NOT NULL,                     -- the SQL of this step
    row_count    BIGINT,                                    -- rows returned by this step
    sum_value    NUMERIC,                                   -- SUM of the checked column after this step
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_steps_run_step UNIQUE (run_id, step_number),
    CONSTRAINT chk_steps_row_count CHECK (row_count IS NULL OR row_count >= 0),
    CONSTRAINT chk_steps_step_type
        CHECK (step_type IN ('FROM', 'JOIN', 'WHERE', 'GROUP_BY', 'AGGREGATE', 'SELECT'))
);

-- 4. CASES: each bug found in a run, with its fix and proof
--    (written by the detectors and the explainer / fix proof)
CREATE TABLE IF NOT EXISTS bronze.cases (
    case_id        INT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id         INT          NOT NULL
                       REFERENCES bronze.runs(run_id) ON DELETE CASCADE,
    step_id        INT
                       REFERENCES bronze.steps(step_id) ON DELETE SET NULL,
                                                            -- step where the bug appeared (optional)
    detector_name  VARCHAR(100) NOT NULL,                   -- which detector found the bug
    bug_type       VARCHAR(100) NOT NULL,                   -- e.g. JOIN_FAN_OUT
    severity       VARCHAR(50)  NOT NULL,
    description    TEXT,                                    -- explanation of the bug
    fix_sql        TEXT,                                    -- the corrected query
    result_before  JSONB,                                   -- result of the original query
    result_after   JSONB,                                   -- result of the fixed query
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_cases_severity
        CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL'))
);

-- 5. Indexes
CREATE INDEX IF NOT EXISTS idx_bronze_runs_executed_at ON bronze.runs(executed_at);
CREATE INDEX IF NOT EXISTS idx_bronze_runs_status      ON bronze.runs(status);
CREATE INDEX IF NOT EXISTS idx_bronze_cases_run_id     ON bronze.cases(run_id);
CREATE INDEX IF NOT EXISTS idx_bronze_cases_step_id    ON bronze.cases(step_id);
-- steps(run_id) is already covered by the unique constraint on (run_id, step_number)