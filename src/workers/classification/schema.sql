-- Run after the collector's schema.sql and ticket_schema.sql, in one transaction.
-- Serialise concurrent init containers without blocking read/write traffic on tickets.
SELECT pg_advisory_xact_lock(hashtextextended('voc.classification.schema.v1', 0));

CREATE TABLE IF NOT EXISTS voc.classification_category_map (
    domain_id uuid NOT NULL,
    label text NOT NULL,
    category_id uuid NOT NULL,
    PRIMARY KEY (domain_id, label),
    FOREIGN KEY (domain_id, category_id) REFERENCES public.category(domain_id, category_id)
);

CREATE TABLE IF NOT EXISTS voc.classification_job (
    request_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL,
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
    UNIQUE (company_id,event_id,preprocess_version,body_version,analysis_kind),
    FOREIGN KEY (company_id,event_id,preprocess_version,body_version)
        REFERENCES voc.model_text_history(company_id,event_id,preprocess_version,body_version)
);
CREATE INDEX IF NOT EXISTS classification_ready ON voc.classification_job(company_id,ready_at,queued_at)
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
    ticket_id uuid NOT NULL REFERENCES public.ticket ON DELETE CASCADE,
    analysis_kind text NOT NULL CHECK (analysis_kind IN ('sentiment','category')),
    request_id uuid NOT NULL REFERENCES voc.classification_job,
    value text,
    status text NOT NULL,
    PRIMARY KEY (ticket_id, analysis_kind)
);

ALTER TABLE voc.classification_job ADD COLUMN IF NOT EXISTS assignment_checked_at timestamptz;
CREATE INDEX IF NOT EXISTS classification_assignment_queue
    ON voc.classification_job(company_id,model_version,finished_at)
    WHERE analysis_kind='category' AND state='succeeded' AND applied AND assignment_checked_at IS NULL;

ALTER TABLE voc.classification_job ADD COLUMN IF NOT EXISTS uses_customer_features boolean NOT NULL DEFAULT false;
ALTER TABLE voc.classification_job ADD COLUMN IF NOT EXISTS feature_snapshot_id uuid;
ALTER TABLE voc.classification_attempt ADD COLUMN IF NOT EXISTS http_ms double precision
    CHECK (http_ms>=0 AND http_ms<'Infinity'::double precision);
-- Billing integrations can attach measured per-attempt cost in KRW.
ALTER TABLE voc.classification_attempt ADD COLUMN IF NOT EXISTS cost_krw numeric CHECK (cost_krw>=0);
CREATE INDEX IF NOT EXISTS classification_company_window ON voc.classification_job(company_id,analysis_kind,model_version);
CREATE INDEX IF NOT EXISTS classification_attempt_window ON voc.classification_attempt(started_at);

-- Jobs may retry on stable after a rollback. Keep the actual target of each attempt.
ALTER TABLE voc.classification_attempt ADD COLUMN IF NOT EXISTS assigned_route text;
ALTER TABLE voc.classification_attempt ADD COLUMN IF NOT EXISTS assigned_version text;
UPDATE voc.classification_attempt a SET assigned_route=j.route, assigned_version=j.model_version
FROM voc.classification_job j WHERE j.request_id=a.request_id AND a.assigned_route IS NULL;

-- A trip stays latched across worker restarts and rolling deploys of the same candidate.
CREATE TABLE IF NOT EXISTS voc.classification_rollout (
    company_id uuid NOT NULL REFERENCES public.company,
    scope_key text NOT NULL,
    candidate_version text NOT NULL,
    stable_version text NOT NULL,
    configured_percent smallint NOT NULL CHECK (configured_percent BETWEEN 0 AND 100),
    failures jsonb NOT NULL DEFAULT '{}',
    first_failure_at timestamptz,
    tripped_at timestamptz,
    reason text,
    PRIMARY KEY (company_id, scope_key, candidate_version)
);
