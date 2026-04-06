-- long-running-inference schema
-- Run once via: databricks bundle run schema_migration

-- job_requests: one row per submitted document
CREATE TABLE IF NOT EXISTS job_requests (
    job_id      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    payload     JSONB       NOT NULL,
    -- payload shape: {messages: [...], max_tokens: int, caller_id: str|null}
    status      TEXT        NOT NULL DEFAULT 'PENDING'
        CONSTRAINT chk_job_requests_status
            CHECK (status IN ('PENDING', 'RUNNING', 'STREAMING', 'DONE', 'FAILED')),
    -- state machine: PENDING → RUNNING → STREAMING → DONE | FAILED
    claimed_by  TEXT,       -- lakeflow run_id that owns this job
    error_msg   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- job_chunks: partial results written as tokens stream in
CREATE TABLE IF NOT EXISTS job_chunks (
    chunk_id    BIGSERIAL   PRIMARY KEY,
    job_id      UUID        NOT NULL REFERENCES job_requests(job_id) ON DELETE CASCADE,
    chunk_index INT         NOT NULL,
    content     TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Prevent duplicate chunks on worker retry (ON CONFLICT DO NOTHING in write_chunk)
    CONSTRAINT uq_job_chunks_job_chunk UNIQUE (job_id, chunk_index)
);

-- job_results: final assembled result written once on completion
CREATE TABLE IF NOT EXISTS job_results (
    job_id        UUID        PRIMARY KEY REFERENCES job_requests(job_id) ON DELETE CASCADE,
    full_text     TEXT        NOT NULL,
    total_tokens  INT,
    prompt_tokens INT,
    latency_ms    INT,
    completed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- indexes
CREATE INDEX IF NOT EXISTS idx_job_requests_pending
    ON job_requests (updated_at)
    WHERE status = 'PENDING';

CREATE INDEX IF NOT EXISTS idx_job_requests_running_streaming
    ON job_requests (status, updated_at)
    WHERE status IN ('RUNNING', 'STREAMING');

CREATE INDEX IF NOT EXISTS idx_job_chunks_job
    ON job_chunks (job_id, chunk_index);
