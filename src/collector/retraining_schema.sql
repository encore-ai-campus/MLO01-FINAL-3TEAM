-- Company policy and durable requests used by the local TiCats scheduler.
CREATE TABLE IF NOT EXISTS voc.retraining_policy (
    company_uuid uuid PRIMARY KEY REFERENCES public.company,
    requested_by uuid NOT NULL,
    revision integer NOT NULL CHECK (revision > 0),
    enabled boolean NOT NULL,
    trigger_mode text NOT NULL CHECK (trigger_mode IN ('period','volume','either','both')),
    interval_days integer NOT NULL CHECK (interval_days BETWEEN 1 AND 365),
    min_new_gold integer NOT NULL CHECK (min_new_gold BETWEEN 1 AND 1000000),
    window_start time NOT NULL,
    window_end time NOT NULL CHECK (window_end <> window_start),
    model text NOT NULL,
    eval_dataset text NOT NULL,
    counting_since timestamptz,
    FOREIGN KEY (company_uuid,requested_by) REFERENCES public.staff_user(company_uuid,staff_user_uuid)
);

CREATE TABLE IF NOT EXISTS voc.retraining_dispatch (
    request_id uuid PRIMARY KEY,
    company_uuid uuid NOT NULL REFERENCES voc.retraining_policy,
    revision integer NOT NULL,
    requested_by uuid NOT NULL,
    model text NOT NULL,
    eval_dataset text NOT NULL,
    cutoff timestamptz NOT NULL,
    workflow_name text NOT NULL UNIQUE,
    argo_uid text,
    state text NOT NULL CHECK (state IN ('pending','running','succeeded','failed','cancelled','stopped')),
    note text NOT NULL DEFAULT '',
    finished_at timestamptz,
    FOREIGN KEY (company_uuid,requested_by) REFERENCES public.staff_user(company_uuid,staff_user_uuid)
);
CREATE UNIQUE INDEX IF NOT EXISTS retraining_one_active ON voc.retraining_dispatch(company_uuid)
    WHERE state IN ('pending','running');
CREATE INDEX IF NOT EXISTS retraining_last_request ON voc.retraining_dispatch(company_uuid,cutoff DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint
               WHERE conrelid = 'voc.retraining_dispatch'::regclass AND conname = 'retraining_dispatch_state_check'
                 AND position('stopped' in pg_get_constraintdef(oid)) = 0) THEN
        ALTER TABLE voc.retraining_dispatch DROP CONSTRAINT retraining_dispatch_state_check,
            ADD CONSTRAINT retraining_dispatch_state_check
            CHECK (state IN ('pending','running','succeeded','failed','cancelled','stopped'));
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS voc.training_stop_request (
    workflow_name text PRIMARY KEY,
    workflow_uid text NOT NULL,
    company_uuid uuid NOT NULL REFERENCES public.company,
    requested_by uuid NOT NULL,
    requested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    state text NOT NULL DEFAULT 'requested' CHECK (state IN ('requested','stopped','completed')),
    confirmed_at timestamptz,
    cutoff timestamptz NOT NULL,
    CHECK ((state = 'requested') = (confirmed_at IS NULL)),
    FOREIGN KEY (company_uuid,requested_by) REFERENCES public.staff_user(company_uuid,staff_user_uuid)
);
CREATE INDEX IF NOT EXISTS training_stop_pending ON voc.training_stop_request(company_uuid, requested_at)
    WHERE state='requested';
CREATE INDEX IF NOT EXISTS training_stop_confirmed ON voc.training_stop_request(company_uuid, requested_at DESC)
    WHERE state='stopped';
