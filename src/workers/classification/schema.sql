-- Run after the collector's schema.sql and ticket_schema.sql, in one transaction.
-- Serialise concurrent init containers without blocking read/write traffic on tickets.
SELECT pg_advisory_xact_lock(hashtextextended('voc.classification.schema.v1', 0));

CREATE TABLE IF NOT EXISTS voc.classification_category_map (
    domain_uuid uuid NOT NULL,
    label text NOT NULL,
    category_uuid uuid NOT NULL,
    PRIMARY KEY (domain_uuid, label),
    FOREIGN KEY (domain_uuid, category_uuid) REFERENCES public.category(domain_uuid, category_uuid)
);

CREATE TABLE IF NOT EXISTS voc.classification_job (
    request_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_uuid uuid NOT NULL,
    event_id uuid NOT NULL,
    preprocess_version integer NOT NULL,
    body_version integer NOT NULL,
    analysis_kind text NOT NULL CHECK (analysis_kind IN ('sentiment','category')),
    route text NOT NULL CHECK (route IN ('stable','canary')),
    model_version text NOT NULL,
    model_url text NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','running','succeeded','failed','skipped')),
    attempt_count integer NOT NULL DEFAULT 0,
    ready_at timestamptz NOT NULL DEFAULT now(),
    queued_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    label text,
    applied boolean NOT NULL DEFAULT false,
    UNIQUE (company_uuid,event_id,preprocess_version,body_version,analysis_kind),
    FOREIGN KEY (company_uuid,event_id,preprocess_version,body_version)
        REFERENCES voc.model_text_history(company_uuid,event_id,preprocess_version,body_version)
);
CREATE INDEX IF NOT EXISTS classification_ready ON voc.classification_job(company_uuid,ready_at,queued_at)
    WHERE state IN ('pending','running');

CREATE TABLE IF NOT EXISTS voc.classification_attempt (
    request_id uuid NOT NULL REFERENCES voc.classification_job,
    attempt_no integer NOT NULL,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    outcome text CHECK (outcome IN ('succeeded','failed','expired')),
    error_code text,
    served_version text,
    PRIMARY KEY (request_id, attempt_no)
);

-- A worker may replace only the ticket value and state it last wrote.
CREATE TABLE IF NOT EXISTS voc.classification_projection (
    ticket_uuid uuid NOT NULL REFERENCES public.ticket ON DELETE CASCADE,
    analysis_kind text NOT NULL CHECK (analysis_kind IN ('sentiment','category')),
    request_id uuid NOT NULL REFERENCES voc.classification_job,
    value text,
    status text NOT NULL,
    PRIMARY KEY (ticket_uuid, analysis_kind)
);

ALTER TABLE voc.classification_job ADD COLUMN IF NOT EXISTS assignment_checked_at timestamptz;
CREATE INDEX IF NOT EXISTS classification_assignment_queue
    ON voc.classification_job(company_uuid,model_version,finished_at)
    WHERE analysis_kind='category' AND state='succeeded' AND applied AND assignment_checked_at IS NULL;
