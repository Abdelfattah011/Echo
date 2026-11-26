-- =====================================================================
-- Migration 002: let the Bronze layer accept the real ECHO engine output
-- Run AFTER 001_bronze_layer_schema.sql. Existing rows are kept.
-- Safe to run more than once.
--
-- Why: M1's parser/runner produces more step types than the six allowed
-- in 001, steps without SQL text (syntax errors), and extra information
-- (scope, status, evidence ...). Bronze is the RAW layer, so it must accept
-- all of it. Quality checks (allowed values etc.) move to dbt in Silver.
-- =====================================================================

-- 1. STEPS ------------------------------------------------------------
-- M1 uses BASE_TABLE, PROJECTION, CTE, UNION_PART, SET_OPERATION, QUERY,
-- DML, DDL, SYNTAX_ERROR, SECURITY_ERROR, ENGINE_ERROR ... not only the old six.
ALTER TABLE bronze.steps DROP CONSTRAINT IF EXISTS chk_steps_step_type;

-- Failed / skipped steps (e.g. syntax errors) have no SQL snippet.
ALTER TABLE bronze.steps ALTER COLUMN sql_snippet DROP NOT NULL;

ALTER TABLE bronze.steps
    ADD COLUMN IF NOT EXISTS step_key          VARCHAR(100),  -- M1's id, e.g. 'step_2'
    ADD COLUMN IF NOT EXISTS scope             TEXT,          -- 'main', a CTE name, ...
    ADD COLUMN IF NOT EXISTS status            VARCHAR(20),   -- success | failed | skipped
    ADD COLUMN IF NOT EXISTS error             TEXT,
    ADD COLUMN IF NOT EXISTS execution_time_ms NUMERIC,
    ADD COLUMN IF NOT EXISTS columns_metrics   JSONB;

-- 2. RUNS -------------------------------------------------------------
ALTER TABLE bronze.runs
    ADD COLUMN IF NOT EXISTS verdict     VARCHAR(30),         -- issues_found | no_issues_found | error
    ADD COLUMN IF NOT EXISTS raw_result  JSONB;               -- the whole engine output, untouched

-- 3. CASES (one row per bug found) --------------------------------------
ALTER TABLE bronze.cases
    ADD COLUMN IF NOT EXISTS bug_category        VARCHAR(100), -- standard name from bug_taxonomy.md (NULL = not one of the 5)
    ADD COLUMN IF NOT EXISTS confidence           VARCHAR(20),
    ADD COLUMN IF NOT EXISTS scope                TEXT,
    ADD COLUMN IF NOT EXISTS suspicious_step_key  VARCHAR(100), -- M1's id, kept even if step_id is NULL
    ADD COLUMN IF NOT EXISTS suggested_fix        TEXT,
    ADD COLUMN IF NOT EXISTS evidence             JSONB;       -- different shape for each detector

-- 4. Index for the dashboard / dbt ------------------------------------
CREATE INDEX IF NOT EXISTS idx_bronze_cases_bug_type     ON bronze.cases(bug_type);
CREATE INDEX IF NOT EXISTS idx_bronze_cases_bug_category ON bronze.cases(bug_category);
