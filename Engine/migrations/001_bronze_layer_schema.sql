create database Echo
use Echo

-- Migration: Bronze Layer Schema for Echo Engine (SQL Server Compatible)

-- 1. Create the dedicated schema for the Bronze layer safely in SQL Server
IF NOT EXISTS (SELECT * FROM sys.schemas WHERE name = 'bronze')
BEGIN
    EXEC('CREATE SCHEMA bronze')
END
GO

-- 2. Runs Table (Stores raw execution sessions)
CREATE TABLE bronze.runs (
    run_id INT IDENTITY(1,1) PRIMARY KEY,
    query_text NVARCHAR(MAX) NOT NULL,
    executed_at DATETIMEOFFSET DEFAULT SYSDATETIMEOFFSET(),
    status VARCHAR(50) NOT NULL DEFAULT 'PENDING'
);
GO

-- 3. Cases Table (Stores raw detected bugs and anomalies per run)
CREATE TABLE bronze.cases (
    case_id INT IDENTITY(1,1) PRIMARY KEY,
    run_id INT FOREIGN KEY REFERENCES bronze.runs(run_id) ON DELETE CASCADE,
    bug_type VARCHAR(100) NOT NULL,
    severity VARCHAR(50) NOT NULL,
    description NVARCHAR(MAX),
    created_at DATETIMEOFFSET DEFAULT SYSDATETIMEOFFSET()
);
GO

-- 4. Steps Table (Stores raw parsed structural steps)
CREATE TABLE bronze.steps (
    step_id INT IDENTITY(1,1) PRIMARY KEY,
    run_id INT FOREIGN KEY REFERENCES bronze.runs(run_id) ON DELETE CASCADE,
    step_number INT NOT NULL,
    step_type VARCHAR(50) NOT NULL,
    step_name VARCHAR(100),
    sql_snippet NVARCHAR(MAX) NOT NULL,
    created_at DATETIMEOFFSET DEFAULT SYSDATETIMEOFFSET()
);
GO

-- Create performance indexes inside the bronze schema
CREATE INDEX idx_bronze_runs_executed_at ON bronze.runs(executed_at);
CREATE INDEX idx_bronze_cases_run_id ON bronze.cases(run_id);
CREATE INDEX idx_bronze_steps_run_id ON bronze.steps(run_id);
GO
