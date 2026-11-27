-- Apply after ticket_schema.sql in the same migration transaction.
SELECT pg_advisory_xact_lock(hashtextextended('voc.training.schema.v1', 0));

ALTER TABLE public.ticket_message ADD COLUMN IF NOT EXISTS sent_at timestamptz;
CREATE INDEX IF NOT EXISTS ticket_first_customer_reply ON public.ticket_message(ticket_id, sent_at)
    WHERE sender_type='staff' AND message_type='reply' AND delivery_status='sent';

CREATE TABLE IF NOT EXISTS voc.training_capture (
    is_singleton boolean PRIMARY KEY DEFAULT true CHECK (is_singleton),
    enabled_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS voc.training_ticket_baseline (
    ticket_id uuid PRIMARY KEY REFERENCES public.ticket,
    status text NOT NULL
);
-- Capture the operating state once. Earlier tickets do not become training examples.
WITH enabled AS (
    INSERT INTO voc.training_capture(enabled_at) VALUES (clock_timestamp())
    ON CONFLICT DO NOTHING RETURNING enabled_at
)
INSERT INTO voc.training_ticket_baseline(ticket_id, status)
SELECT t.ticket_id,t.status FROM public.ticket t CROSS JOIN enabled
WHERE t.source_event_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS voc.feature_snapshot (
    feature_snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES public.company,
    domain_id uuid NOT NULL REFERENCES public.domain,
    ticket_id uuid REFERENCES public.ticket,
    bronze_event_id uuid REFERENCES voc.bronze_message(event_id),
    silver_event_id uuid,
    silver_schema_version integer,
    body_version integer,
    preprocess_version integer,
    source_conversation text,
    seed_ref text,
    title text,
    body text NOT NULL CHECK (btrim(body)<>''),
    unresolved_count bigint CHECK (unresolved_count>=0),
    avg_first_reply_7d_ms double precision CHECK (avg_first_reply_7d_ms>=0
        AND avg_first_reply_7d_ms<'Infinity'::double precision),
    received_at timestamptz NOT NULL,
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    calculation_version text NOT NULL DEFAULT 'customer-history-v1',
    calculation_sources jsonb NOT NULL DEFAULT '{}',
    CHECK (available_at>=received_at),
    CHECK (
        (ticket_id IS NOT NULL AND bronze_event_id IS NOT NULL AND silver_event_id IS NOT NULL
            AND silver_schema_version IS NOT NULL AND body_version IS NOT NULL
            AND preprocess_version IS NOT NULL AND seed_ref IS NULL)
        OR
        (ticket_id IS NULL AND bronze_event_id IS NULL AND silver_event_id IS NULL
            AND silver_schema_version IS NULL AND body_version IS NULL
            AND preprocess_version IS NULL AND seed_ref IS NOT NULL)
    ),
    UNIQUE(company_id,silver_event_id,preprocess_version,body_version),
    UNIQUE(company_id,seed_ref),
    FOREIGN KEY(company_id,silver_event_id,preprocess_version,body_version)
        REFERENCES voc.model_text_history(company_id,event_id,preprocess_version,body_version)
);
CREATE INDEX IF NOT EXISTS feature_snapshot_ticket ON voc.feature_snapshot(ticket_id, available_at, feature_snapshot_id);

CREATE OR REPLACE FUNCTION voc.capture_training_features() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    inquiry record;
    first_snapshot voc.feature_snapshot%ROWTYPE;
    enabled_at timestamptz;
    open_count bigint;
    average_reply double precision;
    sources jsonb;
BEGIN
    SELECT c.enabled_at INTO STRICT enabled_at FROM voc.training_capture c;
    SELECT t.ticket_id,t.company_id,t.customer_id,t.domain_id,t.created_at,s.title,
           coalesce(s.source_channel_id,'') || ':' || s.source_conversation_id AS conversation,
           s.source_channel_type,s.source_connection_id
      INTO inquiry
    FROM public.ticket t JOIN voc.silver_message s ON s.event_id=t.source_event_id
    WHERE t.company_id=NEW.company_id AND t.source_event_id=NEW.event_id
      AND s.schema_version=NEW.silver_schema_version AND s.body_version=NEW.body_version
      AND s.message_kind='customer_inquiry' AND t.created_at>=enabled_at;
    IF NOT FOUND THEN
        RETURN NEW;
    END IF;

    SELECT * INTO first_snapshot FROM voc.feature_snapshot
    WHERE ticket_id=inquiry.ticket_id ORDER BY available_at,feature_snapshot_id LIMIT 1;

    IF FOUND THEN
        open_count := first_snapshot.unresolved_count;
        average_reply := first_snapshot.avg_first_reply_7d_ms;
        sources := first_snapshot.calculation_sources;
    ELSE
        WITH previous AS (
            SELECT t.ticket_id,t.created_at,b.status AS baseline_status
            FROM public.ticket t
            LEFT JOIN voc.training_ticket_baseline b USING(ticket_id)
            WHERE t.company_id=inquiry.company_id AND t.customer_id=inquiry.customer_id
              AND t.source_event_id IS NOT NULL AND t.created_at<inquiry.created_at
              AND EXISTS (
                  SELECT 1 FROM voc.model_text_history h
                  WHERE h.company_id=t.company_id AND h.event_id=t.source_event_id
                    AND h.available_at<inquiry.created_at
              )
        ), history AS (
            SELECT p.*, e.ticket_event_id,e.created_at AS status_at,
                coalesce(e.to_status,p.baseline_status,
                    CASE WHEN p.created_at>=enabled_at THEN '미처리' END) AS status,
                m.ticket_message_id,m.sent_at,m.legacy_sent
            FROM previous p
            LEFT JOIN LATERAL (
                SELECT ev.ticket_event_id,ev.created_at,ev.to_status FROM public.ticket_event ev
                WHERE ev.ticket_id=p.ticket_id AND ev.created_at<inquiry.created_at
                  AND (p.baseline_status IS NULL OR ev.created_at>=enabled_at)
                  AND ev.to_status IN ('미처리','보류','답변완료')
                ORDER BY ev.created_at DESC,ev.ticket_event_id DESC LIMIT 1
            ) e ON true
            LEFT JOIN LATERAL (
                SELECT msg.ticket_message_id,msg.sent_at,msg.sent_at IS NULL AS legacy_sent
                FROM public.ticket_message msg
                WHERE msg.ticket_id=p.ticket_id AND msg.sender_type='staff'
                  AND msg.message_type='reply' AND msg.delivery_status='sent'
                  AND (msg.sent_at<inquiry.created_at OR
                       (msg.sent_at IS NULL AND msg.created_at<inquiry.created_at))
                ORDER BY msg.sent_at NULLS FIRST,msg.ticket_message_id LIMIT 1
            ) m ON true
        )
        SELECT
            CASE WHEN NOT bool_or(status IS NULL)
                      OR count(*)=0 THEN count(*) FILTER (WHERE status IN ('미처리','보류')) END,
            CASE WHEN NOT coalesce(bool_or(legacy_sent AND inquiry.created_at-interval '168 hours'<enabled_at), false)
                 THEN 1000 * avg(extract(epoch FROM sent_at-created_at)) FILTER (
                     WHERE sent_at>=greatest(created_at,inquiry.created_at-interval '168 hours')
                 ) END,
            jsonb_build_object(
                'unresolved', coalesce(jsonb_agg(jsonb_build_object(
                    'ticket_uuid',ticket_id,'ticket_event_uuid',ticket_event_id,
                    'status',status,'status_at',coalesce(status_at,
                        CASE WHEN baseline_status IS NOT NULL THEN enabled_at ELSE created_at END)
                ) ORDER BY ticket_id) FILTER (WHERE status IN ('미처리','보류')), '[]'),
                'first_replies', coalesce(jsonb_agg(jsonb_build_object(
                    'ticket_uuid',ticket_id,'ticket_message_uuid',ticket_message_id,
                    'received_at',created_at,'sent_at',sent_at
                ) ORDER BY ticket_id) FILTER (
                    WHERE sent_at>=greatest(created_at,inquiry.created_at-interval '168 hours')
                ), '[]'),
                'unknown_status',coalesce(jsonb_agg(ticket_id ORDER BY ticket_id)
                    FILTER (WHERE status IS NULL), '[]'),
                'unknown_first_reply',coalesce(jsonb_agg(ticket_message_id ORDER BY ticket_id)
                    FILTER (WHERE legacy_sent AND inquiry.created_at-interval '168 hours'<enabled_at), '[]')
            )
        INTO open_count,average_reply,sources FROM history;
    END IF;

    INSERT INTO voc.feature_snapshot(
        company_id,domain_id,ticket_id,bronze_event_id,silver_event_id,silver_schema_version,
        body_version,preprocess_version,source_conversation,title,body,unresolved_count,
        avg_first_reply_7d_ms,received_at,calculation_version,calculation_sources
    ) VALUES (
        NEW.company_id,inquiry.domain_id,inquiry.ticket_id,NEW.event_id,NEW.event_id,NEW.silver_schema_version,
        NEW.body_version,NEW.preprocess_version,
        CASE WHEN inquiry.conversation IS NOT NULL THEN
            jsonb_build_array(inquiry.source_channel_type,inquiry.source_connection_id,inquiry.conversation)::text END,
        inquiry.title,NEW.model_text,open_count,average_reply,inquiry.created_at,
        coalesce(first_snapshot.calculation_version,'customer-history-v1'),sources
    ) ON CONFLICT (company_id,silver_event_id,preprocess_version,body_version) DO NOTHING;
    RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER capture_training_features AFTER INSERT ON voc.model_text_history
    FOR EACH ROW EXECUTE FUNCTION voc.capture_training_features();

CREATE OR REPLACE FUNCTION voc.ticket_feature_uuid(ticket_id uuid) RETURNS uuid LANGUAGE sql STABLE AS $$
    SELECT f.feature_snapshot_id FROM voc.feature_snapshot f
    JOIN voc.silver_message s ON s.event_id=f.silver_event_id AND s.schema_version=f.silver_schema_version
        AND s.body_version=f.body_version AND s.message_kind='customer_inquiry'
    WHERE f.ticket_id=$1 ORDER BY f.preprocess_version DESC LIMIT 1;
$$;
ALTER TABLE public.ticket_message ADD COLUMN IF NOT EXISTS reviewed_feature_id uuid REFERENCES voc.feature_snapshot;

CREATE TABLE IF NOT EXISTS voc.counselor_review (
    review_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    feature_snapshot_id uuid NOT NULL REFERENCES voc.feature_snapshot,
    reviewer_id uuid NOT NULL REFERENCES public.staff_user,
    reviewed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    sentiment text CHECK (sentiment IN ('긍정','중립','부정','매우 부정')),
    category_id uuid REFERENCES public.category,
    is_urgent boolean,
    evidence_ref text NOT NULL CHECK (btrim(evidence_ref)<>''),
    CHECK (sentiment IS NOT NULL OR category_id IS NOT NULL OR is_urgent IS NOT NULL),
    UNIQUE(feature_snapshot_id,reviewer_id,evidence_ref)
);
CREATE TABLE IF NOT EXISTS voc.feature_snapshot_label (
    feature_snapshot_label_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    feature_snapshot_id uuid NOT NULL REFERENCES voc.feature_snapshot,
    label_kind text NOT NULL CHECK (label_kind IN ('sentiment','category')),
    sentiment_value text CHECK (sentiment_value IN ('긍정','중립','부정')),
    category_id uuid REFERENCES public.category,
    label_origin text NOT NULL CHECK (label_origin IN ('counselor','seed','retained')),
    reviewer_id uuid REFERENCES public.staff_user,
    completion_message_id uuid REFERENCES public.ticket_message,
    as_of_at timestamptz NOT NULL,
    confirmed_at timestamptz NOT NULL CHECK (confirmed_at>=as_of_at),
    evidence jsonb NOT NULL,
    CHECK ((label_kind='sentiment' AND sentiment_value IS NOT NULL AND category_id IS NULL)
        OR (label_kind='category' AND category_id IS NOT NULL AND sentiment_value IS NULL)),
    UNIQUE NULLS NOT DISTINCT(feature_snapshot_id,completion_message_id,label_kind)
);
CREATE INDEX IF NOT EXISTS gold_label_confirmed ON voc.feature_snapshot_label(confirmed_at,feature_snapshot_id);

CREATE OR REPLACE FUNCTION voc.capture_reply_gold() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.ticket_message older WHERE older.ticket_id=NEW.ticket_id
          AND older.ticket_message_id<>NEW.ticket_message_id AND older.sender_type='staff'
          AND older.message_type='reply' AND older.delivery_status='sent'
          AND (older.sent_at IS NULL OR older.sent_at<=NEW.sent_at)
    ) THEN
        RETURN NEW;
    END IF;

    WITH current_input AS (
        SELECT f.*,t.sentiment,t.category_id FROM voc.feature_snapshot f JOIN public.ticket t USING(ticket_id)
        WHERE f.feature_snapshot_id=NEW.reviewed_feature_id AND f.ticket_id=NEW.ticket_id
          AND f.available_at<=NEW.sent_at
    ), edits AS (
        SELECT label.kind,label.value,e.ticket_event_id,e.created_at,e.actor_id,
            first_value(label.before) OVER ordered AS original_value,
            row_number() OVER (PARTITION BY label.kind ORDER BY e.created_at DESC,e.ticket_event_id DESC) AS newest
        FROM current_input i JOIN public.ticket_event e USING(ticket_id)
        JOIN public.staff_user staff ON staff.staff_user_id=e.actor_id AND staff.company_id=i.company_id
        JOIN public.event_type type USING(event_type_id)
        CROSS JOIN LATERAL (VALUES
            ('sentiment','ticket_sentiment_changed',i.sentiment,e.detail_json->>'sentiment_before',e.detail_json->>'sentiment_after'),
            ('category','ticket_category_changed',i.category_id::text,e.detail_json->>'category_before',e.detail_json->>'category_after')
        ) label(kind,code,value,before,after)
        WHERE type.code=label.code AND e.detail_json->>'feature_uuid'=i.feature_snapshot_id::text
          AND e.created_at BETWEEN i.available_at AND NEW.sent_at
          AND label.before IS DISTINCT FROM label.after
        WINDOW ordered AS (PARTITION BY label.kind ORDER BY e.created_at,e.ticket_event_id)
    ), candidates AS (
        SELECT kind,value,created_at AS reviewed_at,actor_id AS reviewer_id,
               jsonb_build_object('ticket_event_uuid',ticket_event_id) AS evidence
        FROM edits WHERE newest=1 AND value IS DISTINCT FROM original_value
          AND EXISTS (SELECT 1 FROM public.ticket_event e WHERE e.ticket_event_id=edits.ticket_event_id
              AND e.detail_json->>(edits.kind || '_after')=edits.value)
        UNION ALL
        SELECT label.kind,label.value,r.reviewed_at,r.reviewer_id,
               jsonb_build_object('review_uuid',r.review_id,'ref',r.evidence_ref,'is_urgent',r.is_urgent)
        FROM current_input i JOIN voc.counselor_review r USING(feature_snapshot_id)
        JOIN public.staff_user staff ON staff.staff_user_id=r.reviewer_id AND staff.company_id=i.company_id
        CROSS JOIN LATERAL (VALUES
            ('sentiment',r.sentiment,i.sentiment),('category',r.category_id::text,i.category_id::text)
        ) label(kind,value,current_value)
        WHERE r.reviewed_at BETWEEN i.available_at AND NEW.sent_at AND label.value=label.current_value
    )
    INSERT INTO voc.feature_snapshot_label(feature_snapshot_id,label_kind,sentiment_value,category_id,label_origin,
        reviewer_id,completion_message_id,as_of_at,confirmed_at,evidence)
    SELECT DISTINCT ON (c.kind) i.feature_snapshot_id,c.kind,
        CASE WHEN c.kind='sentiment' THEN CASE c.value WHEN '매우 부정' THEN '부정' ELSE c.value END END,
        CASE WHEN c.kind='category' THEN c.value::uuid END,
        'counselor',c.reviewer_id,NEW.ticket_message_id,c.reviewed_at,NEW.sent_at,c.evidence
    FROM current_input i CROSS JOIN candidates c
    WHERE (c.kind='sentiment' AND c.value IN ('긍정','중립','부정','매우 부정'))
       OR (c.kind='category' AND EXISTS (SELECT 1 FROM public.category a
           WHERE a.category_id=i.category_id AND a.domain_id=i.domain_id))
    ORDER BY c.kind,c.reviewed_at DESC,c.evidence::text
    ON CONFLICT DO NOTHING;
    RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER capture_reply_gold AFTER UPDATE OF sent_at ON public.ticket_message
    FOR EACH ROW WHEN (OLD.sent_at IS NULL AND NEW.sent_at IS NOT NULL AND NEW.sender_type='staff'
        AND NEW.message_type='reply' AND NEW.delivery_status='sent' AND NEW.reviewed_feature_id IS NOT NULL)
    EXECUTE FUNCTION voc.capture_reply_gold();

CREATE SEQUENCE IF NOT EXISTS voc.training_dataset_number;
CREATE TABLE IF NOT EXISTS voc.training_dataset (
    dataset_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES public.company,
    dataset_version text NOT NULL UNIQUE,
    purpose text NOT NULL CHECK (purpose IN ('train','eval')),
    selection_sha256 text NOT NULL,
    snapshot_ref text NOT NULL,
    sha256 text,
    row_count integer CHECK (row_count>0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finalized_at timestamptz,
    CHECK ((finalized_at IS NULL)=(sha256 IS NULL) AND (sha256 IS NULL)=(row_count IS NULL)),
    UNIQUE(company_id,purpose,selection_sha256)
);
CREATE TABLE IF NOT EXISTS voc.training_dataset_item (
    dataset_id uuid NOT NULL REFERENCES voc.training_dataset,
    feature_snapshot_id uuid NOT NULL REFERENCES voc.feature_snapshot,
    as_of_at timestamptz NOT NULL,
    sentiment_label_id uuid REFERENCES voc.feature_snapshot_label,
    category_label_id uuid REFERENCES voc.feature_snapshot_label,
    CHECK (sentiment_label_id IS NOT NULL OR category_label_id IS NOT NULL),
    PRIMARY KEY(dataset_id,feature_snapshot_id)
);
CREATE TABLE IF NOT EXISTS voc.training_run (
    workflow_ref text PRIMARY KEY,
    train_dataset_id uuid NOT NULL REFERENCES voc.training_dataset,
    eval_dataset_id uuid NOT NULL REFERENCES voc.training_dataset,
    base_model_uri text NOT NULL,
    label_cutoff_at timestamptz,
    mlflow_run_id text UNIQUE,
    logged_model_uri text,
    model_name text NOT NULL,
    sentiment_macro_f1 double precision,
    category_macro_f1 double precision
);
CREATE TABLE IF NOT EXISTS voc.model_version (
    workflow_ref text PRIMARY KEY REFERENCES voc.training_run,
    model_name text NOT NULL,
    registry_version integer NOT NULL CHECK (registry_version>0),
    artifact_ref text NOT NULL,
    UNIQUE(model_name,registry_version)
);
ALTER TABLE voc.training_run ADD COLUMN IF NOT EXISTS review_counts jsonb NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS voc.training_progress (
    workflow_ref text NOT NULL,
    node_id text NOT NULL,
    operation text NOT NULL,
    state text NOT NULL CHECK (state IN ('requested','running','completed','failed','reused')),
    reason text,
    paths jsonb NOT NULL DEFAULT '{}',
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (workflow_ref,node_id,operation)
);
