-- Apply after ticket_schema.sql in the same migration transaction.
SELECT pg_advisory_xact_lock(hashtextextended('voc.training.schema.v1', 0));

ALTER TABLE public.ticket_message ADD COLUMN IF NOT EXISTS sent_at timestamptz;
CREATE INDEX IF NOT EXISTS ticket_first_customer_reply ON public.ticket_message(ticket_uuid, sent_at)
    WHERE sender_type='staff' AND message_type='reply' AND delivery_status='sent';

CREATE TABLE IF NOT EXISTS voc.training_capture (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    enabled_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS voc.training_ticket_baseline (
    ticket_uuid uuid PRIMARY KEY REFERENCES public.ticket,
    status text NOT NULL
);
-- Capture the operating state once. Earlier tickets do not become training examples.
WITH enabled AS (
    INSERT INTO voc.training_capture(enabled_at) VALUES (clock_timestamp())
    ON CONFLICT DO NOTHING RETURNING enabled_at
)
INSERT INTO voc.training_ticket_baseline(ticket_uuid, status)
SELECT t.ticket_uuid,t.status FROM public.ticket t CROSS JOIN enabled
WHERE t.source_event_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS voc.feature_snapshot (
    feature_uuid uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_uuid uuid NOT NULL REFERENCES public.company,
    domain_uuid uuid NOT NULL REFERENCES public.domain,
    ticket_uuid uuid REFERENCES public.ticket,
    bronze_event_id uuid REFERENCES voc.bronze_messages(event_id),
    silver_event_id uuid,
    silver_schema_version integer,
    body_version integer,
    preprocess_version integer,
    source_conversation text,
    seed_ref text,
    title text,
    body text NOT NULL CHECK (btrim(body)<>''),
    unresolved_count bigint CHECK (unresolved_count>=0),
    avg_first_reply_7d_seconds double precision CHECK (avg_first_reply_7d_seconds>=0
        AND avg_first_reply_7d_seconds<'Infinity'::double precision),
    received_at timestamptz NOT NULL,
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    calculation_version text NOT NULL DEFAULT 'customer-history-v1',
    calculation_sources jsonb NOT NULL DEFAULT '{}',
    CHECK (available_at>=received_at),
    CHECK (
        (ticket_uuid IS NOT NULL AND bronze_event_id IS NOT NULL AND silver_event_id IS NOT NULL
            AND silver_schema_version IS NOT NULL AND body_version IS NOT NULL
            AND preprocess_version IS NOT NULL AND seed_ref IS NULL)
        OR
        (ticket_uuid IS NULL AND bronze_event_id IS NULL AND silver_event_id IS NULL
            AND silver_schema_version IS NULL AND body_version IS NULL
            AND preprocess_version IS NULL AND seed_ref IS NOT NULL)
    ),
    UNIQUE(company_uuid,silver_event_id,preprocess_version,body_version),
    UNIQUE(company_uuid,seed_ref),
    FOREIGN KEY(company_uuid,silver_event_id,preprocess_version,body_version)
        REFERENCES voc.model_text_history(company_uuid,event_id,preprocess_version,body_version)
);
CREATE INDEX IF NOT EXISTS feature_snapshot_ticket ON voc.feature_snapshot(ticket_uuid, available_at, feature_uuid);

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
    SELECT t.ticket_uuid,t.company_uuid,t.customer_uuid,t.domain_uuid,t.created_at,s.title,
           coalesce(s.source_channel_id,'') || ':' || s.source_conversation_id AS conversation,
           s.channel,s.connection_id
      INTO inquiry
    FROM public.ticket t JOIN voc.silver_messages s ON s.event_id=t.source_event_id
    WHERE t.company_uuid=NEW.company_uuid AND t.source_event_id=NEW.event_id
      AND s.schema_version=NEW.silver_schema_version AND s.body_version=NEW.body_version
      AND s.message_kind='customer_inquiry' AND t.created_at>=enabled_at;
    IF NOT FOUND THEN
        RETURN NEW;
    END IF;

    SELECT * INTO first_snapshot FROM voc.feature_snapshot
    WHERE ticket_uuid=inquiry.ticket_uuid ORDER BY available_at,feature_uuid LIMIT 1;

    IF FOUND THEN
        open_count := first_snapshot.unresolved_count;
        average_reply := first_snapshot.avg_first_reply_7d_seconds;
        sources := first_snapshot.calculation_sources;
    ELSE
        WITH previous AS (
            SELECT t.ticket_uuid,t.created_at,b.status AS baseline_status
            FROM public.ticket t
            LEFT JOIN voc.training_ticket_baseline b USING(ticket_uuid)
            WHERE t.company_uuid=inquiry.company_uuid AND t.customer_uuid=inquiry.customer_uuid
              AND t.source_event_id IS NOT NULL AND t.created_at<inquiry.created_at
              AND EXISTS (
                  SELECT 1 FROM voc.model_text_history h
                  WHERE h.company_uuid=t.company_uuid AND h.event_id=t.source_event_id
                    AND h.available_at<inquiry.created_at
              )
        ), history AS (
            SELECT p.*, e.ticket_event_uuid,e.created_at AS status_at,
                coalesce(e.to_status,p.baseline_status,
                    CASE WHEN p.created_at>=enabled_at THEN '미처리' END) AS status,
                m.ticket_message_uuid,m.sent_at,m.legacy_sent
            FROM previous p
            LEFT JOIN LATERAL (
                SELECT ev.ticket_event_uuid,ev.created_at,ev.to_status FROM public.ticket_event ev
                WHERE ev.ticket_uuid=p.ticket_uuid AND ev.created_at<inquiry.created_at
                  AND (p.baseline_status IS NULL OR ev.created_at>=enabled_at)
                  AND ev.to_status IN ('미처리','보류','답변완료')
                ORDER BY ev.created_at DESC,ev.ticket_event_uuid DESC LIMIT 1
            ) e ON true
            LEFT JOIN LATERAL (
                SELECT msg.ticket_message_uuid,msg.sent_at,msg.sent_at IS NULL AS legacy_sent
                FROM public.ticket_message msg
                WHERE msg.ticket_uuid=p.ticket_uuid AND msg.sender_type='staff'
                  AND msg.message_type='reply' AND msg.delivery_status='sent'
                  AND (msg.sent_at<inquiry.created_at OR
                       (msg.sent_at IS NULL AND msg.created_at<inquiry.created_at))
                ORDER BY msg.sent_at NULLS FIRST,msg.ticket_message_uuid LIMIT 1
            ) m ON true
        )
        SELECT
            CASE WHEN NOT bool_or(status IS NULL)
                      OR count(*)=0 THEN count(*) FILTER (WHERE status IN ('미처리','보류')) END,
            CASE WHEN NOT coalesce(bool_or(legacy_sent AND inquiry.created_at-interval '168 hours'<enabled_at), false)
                 THEN avg(extract(epoch FROM sent_at-created_at)) FILTER (
                     WHERE sent_at>=greatest(created_at,inquiry.created_at-interval '168 hours')
                 ) END,
            jsonb_build_object(
                'unresolved', coalesce(jsonb_agg(jsonb_build_object(
                    'ticket_uuid',ticket_uuid,'ticket_event_uuid',ticket_event_uuid,
                    'status',status,'status_at',coalesce(status_at,
                        CASE WHEN baseline_status IS NOT NULL THEN enabled_at ELSE created_at END)
                ) ORDER BY ticket_uuid) FILTER (WHERE status IN ('미처리','보류')), '[]'),
                'first_replies', coalesce(jsonb_agg(jsonb_build_object(
                    'ticket_uuid',ticket_uuid,'ticket_message_uuid',ticket_message_uuid,
                    'received_at',created_at,'sent_at',sent_at
                ) ORDER BY ticket_uuid) FILTER (
                    WHERE sent_at>=greatest(created_at,inquiry.created_at-interval '168 hours')
                ), '[]'),
                'unknown_status',coalesce(jsonb_agg(ticket_uuid ORDER BY ticket_uuid)
                    FILTER (WHERE status IS NULL), '[]'),
                'unknown_first_reply',coalesce(jsonb_agg(ticket_message_uuid ORDER BY ticket_uuid)
                    FILTER (WHERE legacy_sent AND inquiry.created_at-interval '168 hours'<enabled_at), '[]')
            )
        INTO open_count,average_reply,sources FROM history;
    END IF;

    INSERT INTO voc.feature_snapshot(
        company_uuid,domain_uuid,ticket_uuid,bronze_event_id,silver_event_id,silver_schema_version,
        body_version,preprocess_version,source_conversation,title,body,unresolved_count,
        avg_first_reply_7d_seconds,received_at,calculation_version,calculation_sources
    ) VALUES (
        NEW.company_uuid,inquiry.domain_uuid,inquiry.ticket_uuid,NEW.event_id,NEW.event_id,NEW.silver_schema_version,
        NEW.body_version,NEW.preprocess_version,
        CASE WHEN inquiry.conversation IS NOT NULL THEN
            jsonb_build_array(inquiry.channel,inquiry.connection_id,inquiry.conversation)::text END,
        inquiry.title,NEW.model_text,open_count,average_reply,inquiry.created_at,
        coalesce(first_snapshot.calculation_version,'customer-history-v1'),sources
    ) ON CONFLICT (company_uuid,silver_event_id,preprocess_version,body_version) DO NOTHING;
    RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER capture_training_features AFTER INSERT ON voc.model_text_history
    FOR EACH ROW EXECUTE FUNCTION voc.capture_training_features();

CREATE OR REPLACE FUNCTION voc.ticket_feature_uuid(ticket_id uuid) RETURNS uuid LANGUAGE sql STABLE AS $$
    SELECT f.feature_uuid FROM voc.feature_snapshot f
    JOIN voc.silver_messages s ON s.event_id=f.silver_event_id AND s.schema_version=f.silver_schema_version
        AND s.body_version=f.body_version AND s.message_kind='customer_inquiry'
    WHERE f.ticket_uuid=ticket_id ORDER BY f.preprocess_version DESC LIMIT 1;
$$;
ALTER TABLE public.ticket_message ADD COLUMN IF NOT EXISTS reviewed_feature_uuid uuid REFERENCES voc.feature_snapshot;

CREATE TABLE IF NOT EXISTS voc.counselor_review (
    review_uuid uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    feature_uuid uuid NOT NULL REFERENCES voc.feature_snapshot,
    reviewer_uuid uuid NOT NULL REFERENCES public.staff_user,
    reviewed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    sentiment text CHECK (sentiment IN ('긍정','중립','부정','매우 부정')),
    category_uuid uuid REFERENCES public.category,
    is_urgent boolean,
    evidence_ref text NOT NULL CHECK (btrim(evidence_ref)<>''),
    CHECK (sentiment IS NOT NULL OR category_uuid IS NOT NULL OR is_urgent IS NOT NULL),
    UNIQUE(feature_uuid,reviewer_uuid,evidence_ref)
);
CREATE TABLE IF NOT EXISTS voc.gold_label (
    gold_label_uuid uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    feature_uuid uuid NOT NULL REFERENCES voc.feature_snapshot,
    label_kind text NOT NULL CHECK (label_kind IN ('sentiment','category')),
    sentiment_value text CHECK (sentiment_value IN ('긍정','중립','부정')),
    category_uuid uuid REFERENCES public.category,
    label_origin text NOT NULL CHECK (label_origin IN ('counselor','seed','retained')),
    reviewer_uuid uuid REFERENCES public.staff_user,
    completion_message_uuid uuid REFERENCES public.ticket_message,
    as_of_at timestamptz NOT NULL,
    confirmed_at timestamptz NOT NULL CHECK (confirmed_at>=as_of_at),
    evidence jsonb NOT NULL,
    CHECK ((label_kind='sentiment' AND sentiment_value IS NOT NULL AND category_uuid IS NULL)
        OR (label_kind='category' AND category_uuid IS NOT NULL AND sentiment_value IS NULL)),
    UNIQUE NULLS NOT DISTINCT(feature_uuid,completion_message_uuid,label_kind)
);
CREATE INDEX IF NOT EXISTS gold_label_confirmed ON voc.gold_label(confirmed_at,feature_uuid);

CREATE OR REPLACE FUNCTION voc.capture_reply_gold() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.ticket_message older WHERE older.ticket_uuid=NEW.ticket_uuid
          AND older.ticket_message_uuid<>NEW.ticket_message_uuid AND older.sender_type='staff'
          AND older.message_type='reply' AND older.delivery_status='sent'
          AND (older.sent_at IS NULL OR older.sent_at<=NEW.sent_at)
    ) THEN
        RETURN NEW;
    END IF;

    WITH current_input AS (
        SELECT f.*,t.sentiment,t.category_uuid FROM voc.feature_snapshot f JOIN public.ticket t USING(ticket_uuid)
        WHERE f.feature_uuid=NEW.reviewed_feature_uuid AND f.ticket_uuid=NEW.ticket_uuid
          AND f.available_at<=NEW.sent_at
    ), edits AS (
        SELECT label.kind,label.value,e.ticket_event_uuid,e.created_at,e.actor_uuid,
            first_value(label.before) OVER ordered AS original_value,
            row_number() OVER (PARTITION BY label.kind ORDER BY e.created_at DESC,e.ticket_event_uuid DESC) AS newest
        FROM current_input i JOIN public.ticket_event e USING(ticket_uuid)
        JOIN public.staff_user staff ON staff.staff_user_uuid=e.actor_uuid AND staff.company_uuid=i.company_uuid
        JOIN public.event_type type USING(event_type_uuid)
        CROSS JOIN LATERAL (VALUES
            ('sentiment','ticket_sentiment_changed',i.sentiment,e.details->>'sentiment_before',e.details->>'sentiment_after'),
            ('category','ticket_category_changed',i.category_uuid::text,e.details->>'category_before',e.details->>'category_after')
        ) label(kind,code,value,before,after)
        WHERE type.code=label.code AND e.details->>'feature_uuid'=i.feature_uuid::text
          AND e.created_at BETWEEN i.available_at AND NEW.sent_at
          AND label.before IS DISTINCT FROM label.after
        WINDOW ordered AS (PARTITION BY label.kind ORDER BY e.created_at,e.ticket_event_uuid)
    ), candidates AS (
        SELECT kind,value,created_at AS reviewed_at,actor_uuid AS reviewer_uuid,
               jsonb_build_object('ticket_event_uuid',ticket_event_uuid) AS evidence
        FROM edits WHERE newest=1 AND value IS DISTINCT FROM original_value
          AND EXISTS (SELECT 1 FROM public.ticket_event e WHERE e.ticket_event_uuid=edits.ticket_event_uuid
              AND e.details->>(edits.kind || '_after')=edits.value)
        UNION ALL
        SELECT label.kind,label.value,r.reviewed_at,r.reviewer_uuid,
               jsonb_build_object('review_uuid',r.review_uuid,'ref',r.evidence_ref,'is_urgent',r.is_urgent)
        FROM current_input i JOIN voc.counselor_review r USING(feature_uuid)
        JOIN public.staff_user staff ON staff.staff_user_uuid=r.reviewer_uuid AND staff.company_uuid=i.company_uuid
        CROSS JOIN LATERAL (VALUES
            ('sentiment',r.sentiment,i.sentiment),('category',r.category_uuid::text,i.category_uuid::text)
        ) label(kind,value,current_value)
        WHERE r.reviewed_at BETWEEN i.available_at AND NEW.sent_at AND label.value=label.current_value
    )
    INSERT INTO voc.gold_label(feature_uuid,label_kind,sentiment_value,category_uuid,label_origin,
        reviewer_uuid,completion_message_uuid,as_of_at,confirmed_at,evidence)
    SELECT DISTINCT ON (c.kind) i.feature_uuid,c.kind,
        CASE WHEN c.kind='sentiment' THEN CASE c.value WHEN '매우 부정' THEN '부정' ELSE c.value END END,
        CASE WHEN c.kind='category' THEN c.value::uuid END,
        'counselor',c.reviewer_uuid,NEW.ticket_message_uuid,c.reviewed_at,NEW.sent_at,c.evidence
    FROM current_input i CROSS JOIN candidates c
    WHERE (c.kind='sentiment' AND c.value IN ('긍정','중립','부정','매우 부정'))
       OR (c.kind='category' AND EXISTS (SELECT 1 FROM public.category a
           WHERE a.category_uuid=i.category_uuid AND a.domain_uuid=i.domain_uuid))
    ORDER BY c.kind,c.reviewed_at DESC,c.evidence::text
    ON CONFLICT DO NOTHING;
    RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER capture_reply_gold AFTER UPDATE OF sent_at ON public.ticket_message
    FOR EACH ROW WHEN (OLD.sent_at IS NULL AND NEW.sent_at IS NOT NULL AND NEW.sender_type='staff'
        AND NEW.message_type='reply' AND NEW.delivery_status='sent' AND NEW.reviewed_feature_uuid IS NOT NULL)
    EXECUTE FUNCTION voc.capture_reply_gold();

CREATE SEQUENCE IF NOT EXISTS voc.training_dataset_number;
CREATE TABLE IF NOT EXISTS voc.training_dataset (
    dataset_uuid uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_uuid uuid NOT NULL REFERENCES public.company,
    dataset_version text NOT NULL UNIQUE,
    purpose text NOT NULL CHECK (purpose IN ('train','eval')),
    selection_sha256 text NOT NULL,
    snapshot_ref text NOT NULL,
    sha256 text,
    row_count integer CHECK (row_count>0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finalized_at timestamptz,
    CHECK ((finalized_at IS NULL)=(sha256 IS NULL) AND (sha256 IS NULL)=(row_count IS NULL)),
    UNIQUE(company_uuid,purpose,selection_sha256)
);
CREATE TABLE IF NOT EXISTS voc.training_dataset_item (
    dataset_uuid uuid NOT NULL REFERENCES voc.training_dataset,
    feature_uuid uuid NOT NULL REFERENCES voc.feature_snapshot,
    as_of_at timestamptz NOT NULL,
    sentiment_label_uuid uuid REFERENCES voc.gold_label,
    category_label_uuid uuid REFERENCES voc.gold_label,
    CHECK (sentiment_label_uuid IS NOT NULL OR category_label_uuid IS NOT NULL),
    PRIMARY KEY(dataset_uuid,feature_uuid)
);
CREATE TABLE IF NOT EXISTS voc.training_run (
    workflow_ref text PRIMARY KEY,
    train_dataset_uuid uuid NOT NULL REFERENCES voc.training_dataset,
    eval_dataset_uuid uuid NOT NULL REFERENCES voc.training_dataset,
    base_model_uri text NOT NULL,
    label_cutoff timestamptz,
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
