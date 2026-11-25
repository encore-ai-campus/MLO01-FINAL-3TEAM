CREATE SCHEMA IF NOT EXISTS monitor;

CREATE TABLE IF NOT EXISTS voc.pipeline_stage (
    event_id uuid NOT NULL,
    stage text NOT NULL CHECK (stage IN ('capture','bronze','flink','ticket','compute','ready')),
    started_at timestamptz,
    succeeded_at timestamptz,
    PRIMARY KEY (event_id,stage)
);
CREATE INDEX IF NOT EXISTS pipeline_stage_in ON voc.pipeline_stage(stage,started_at);
CREATE INDEX IF NOT EXISTS pipeline_stage_out ON voc.pipeline_stage(stage,succeeded_at);
CREATE OR REPLACE FUNCTION voc.observe_stage(source_id uuid, step text, entered timestamptz, done timestamptz)
RETURNS void LANGUAGE sql AS $$
    INSERT INTO voc.pipeline_stage AS p(event_id,stage,started_at,succeeded_at)
    VALUES (source_id,step,entered,done)
    ON CONFLICT(event_id,stage) DO UPDATE
    SET started_at=least(p.started_at,excluded.started_at), succeeded_at=least(p.succeeded_at,excluded.succeeded_at)
    WHERE excluded.started_at < p.started_at OR excluded.succeeded_at < p.succeeded_at
       OR (p.started_at IS NULL AND excluded.started_at IS NOT NULL)
       OR (p.succeeded_at IS NULL AND excluded.succeeded_at IS NOT NULL)
$$;

CREATE TABLE IF NOT EXISTS voc.pipeline_kafka (
    topic text NOT NULL, partition_id integer NOT NULL, kafka_offset bigint NOT NULL,
    event_id uuid, stage text NOT NULL, recorded_at timestamptz NOT NULL,
    PRIMARY KEY (topic,partition_id,kafka_offset)
);
CREATE INDEX IF NOT EXISTS pipeline_kafka_source ON voc.pipeline_kafka(event_id,stage);
CREATE TABLE IF NOT EXISTS voc.kafka_progress (
    consumer_group text NOT NULL, topic text NOT NULL, partition_id integer NOT NULL,
    committed_offset bigint, high_watermark bigint NOT NULL, observed_at timestamptz NOT NULL,
    PRIMARY KEY(consumer_group,topic,partition_id)
);
CREATE TABLE IF NOT EXISTS voc.flink_attempt (
    attempt_id uuid PRIMARY KEY, event_id uuid NOT NULL, request_partition integer NOT NULL,
    request_offset bigint NOT NULL, started_at timestamptz NOT NULL, finished_at timestamptz, error text
);
CREATE INDEX IF NOT EXISTS flink_attempt_source ON voc.flink_attempt(event_id,started_at);

CREATE TABLE IF NOT EXISTS voc.pipeline_failure (
    stage text NOT NULL, work_ref text NOT NULL, event_id uuid,
    reason text NOT NULL, failed_at timestamptz NOT NULL DEFAULT clock_timestamp(), recovered_at timestamptz,
    PRIMARY KEY(stage,work_ref)
);
CREATE INDEX IF NOT EXISTS pipeline_failure_source ON voc.pipeline_failure(event_id,stage) WHERE recovered_at IS NULL;
CREATE OR REPLACE FUNCTION voc.fail_stage(step text, ref text, source_id uuid, why text) RETURNS void
LANGUAGE sql AS $$
    INSERT INTO voc.pipeline_failure(stage,work_ref,event_id,reason) VALUES (step,ref,source_id,why)
    ON CONFLICT DO NOTHING
$$;
CREATE OR REPLACE FUNCTION voc.recover_stage(step text, source_id uuid) RETURNS void
LANGUAGE sql AS $$
    UPDATE voc.pipeline_failure SET recovered_at=clock_timestamp()
    WHERE stage=step AND event_id=source_id AND recovered_at IS NULL
$$;

ALTER TABLE public.ticket ADD COLUMN IF NOT EXISTS embedding_model_name text;
ALTER TABLE public.ticket ADD COLUMN IF NOT EXISTS embedding_model_version text;
ALTER TABLE public.ticket ADD COLUMN IF NOT EXISTS embedding_body_version integer;
ALTER TABLE public.ticket ADD COLUMN IF NOT EXISTS ready_at timestamptz;
ALTER TABLE public.ticket ADD COLUMN IF NOT EXISTS classification_final_at timestamptz;
ALTER TABLE voc.model_text_history ADD COLUMN IF NOT EXISTS model_title text;
ALTER TABLE voc.ticket_analysis_outbox DROP CONSTRAINT IF EXISTS ticket_analysis_outbox_analysis_kind_check;
ALTER TABLE voc.ticket_analysis_outbox ADD CONSTRAINT ticket_analysis_outbox_analysis_kind_check
    CHECK(analysis_kind IN ('sentiment','category','embedding'));
ALTER TABLE voc.ticket_analysis_outbox ADD COLUMN IF NOT EXISTS available_at timestamptz NOT NULL DEFAULT clock_timestamp();
ALTER TABLE voc.ticket_analysis_outbox ADD COLUMN IF NOT EXISTS finished_at timestamptz;
ALTER TABLE voc.ticket_analysis_outbox ADD COLUMN IF NOT EXISTS claim_token uuid;
ALTER TABLE voc.ticket_analysis_outbox ADD COLUMN IF NOT EXISTS lease_until timestamptz;
ALTER TABLE voc.ticket_analysis_outbox ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0;
ALTER TABLE voc.ticket_analysis_outbox ADD COLUMN IF NOT EXISTS retry_budget integer NOT NULL DEFAULT 3;
CREATE INDEX IF NOT EXISTS analysis_pending ON voc.ticket_analysis_outbox(analysis_kind,available_at,queued_at)
    WHERE finished_at IS NULL;

CREATE TABLE IF NOT EXISTS voc.inference_attempt (
    attempt_id uuid PRIMARY KEY,
    ticket_uuid uuid REFERENCES public.ticket,
    body_version integer,
    api text NOT NULL CHECK(api IN ('classification','embedding')),
    traffic text NOT NULL CHECK(traffic IN ('operational','replay','evaluation')),
    preferred_route text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(), ended_at timestamptz,
    model_name text, model_version text,
    rollout text CHECK (rollout IN ('stable','canary')),
    http_status integer, response_ms double precision, model_ms double precision, error text
);
ALTER TABLE voc.inference_attempt ADD COLUMN IF NOT EXISTS preprocess_version integer;
ALTER TABLE public.ticket ADD COLUMN IF NOT EXISTS embedding_attempt_id uuid REFERENCES voc.inference_attempt;
CREATE INDEX IF NOT EXISTS inference_attempt_by_time ON voc.inference_attempt(api,traffic,started_at);

CREATE TABLE IF NOT EXISTS voc.ticket_prediction (
    ticket_uuid uuid NOT NULL REFERENCES public.ticket,
    kind text NOT NULL CHECK (kind IN ('sentiment','category')),
    value text NOT NULL,
    attempt_id uuid NOT NULL REFERENCES voc.inference_attempt,
    PRIMARY KEY(ticket_uuid,kind)
);
ALTER TABLE public.ticket ADD COLUMN IF NOT EXISTS reply_delivery_uuid uuid REFERENCES public.ticket_message;
ALTER TABLE public.ticket_message ADD COLUMN IF NOT EXISTS sent_at timestamptz;
CREATE TABLE IF NOT EXISTS voc.prediction_display (
    ticket_uuid uuid NOT NULL, kind text NOT NULL, staff_user_uuid uuid NOT NULL REFERENCES public.staff_user,
    displayed_at timestamptz NOT NULL DEFAULT clock_timestamp(), displayed_value text NOT NULL,
    attempt_id uuid NOT NULL REFERENCES voc.inference_attempt,
    PRIMARY KEY(ticket_uuid,kind,staff_user_uuid),
    FOREIGN KEY(ticket_uuid,kind) REFERENCES voc.ticket_prediction
);
CREATE TABLE IF NOT EXISTS voc.ticket_answer_completion (
    ticket_uuid uuid PRIMARY KEY REFERENCES public.ticket,
    completed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    category_uuid uuid, sentiment text,
    completed_by uuid NOT NULL REFERENCES public.staff_user,
    delivery_uuid uuid NOT NULL REFERENCES public.ticket_message
);

-- The current classifier input uses the same Silver and preprocessing versions as project_ticket.
CREATE OR REPLACE VIEW voc.ticket_training_input AS
SELECT t.ticket_uuid,t.company_uuid,t.domain_uuid,h.event_id,h.body_version,h.preprocess_version,h.available_at
FROM public.ticket t
JOIN voc.silver_messages s ON s.event_id=t.source_event_id AND s.schema_version=4
JOIN voc.model_text_history h ON h.company_uuid=t.company_uuid AND h.event_id=s.event_id
    AND h.body_version=s.body_version AND h.silver_schema_version=s.schema_version
WHERE s.message_kind='customer_inquiry' AND h.preprocess_version=1;

CREATE TABLE IF NOT EXISTS voc.ticket_classification_edit (
    ticket_uuid uuid NOT NULL REFERENCES public.ticket,
    kind text NOT NULL CHECK (kind IN ('sentiment','category')),
    value text, staff_user_uuid uuid NOT NULL REFERENCES public.staff_user,
    body_version integer NOT NULL, preprocess_version integer NOT NULL,
    edited_at timestamptz NOT NULL,
    PRIMARY KEY(ticket_uuid,kind)
);

CREATE TABLE IF NOT EXISTS voc.chat_training_completion (
    completion_uuid uuid PRIMARY KEY,
    conversation_uuid uuid NOT NULL,
    company_uuid uuid NOT NULL REFERENCES public.company,
    domain_uuid uuid NOT NULL REFERENCES public.domain,
    completed_by uuid NOT NULL,
    completed_at timestamptz NOT NULL,
    input_uuid uuid NOT NULL,
    input_version integer NOT NULL CHECK(input_version>0),
    input_sha256 text NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    event_sha256 text NOT NULL CHECK (event_sha256 ~ '^[0-9a-f]{64}$'),
    FOREIGN KEY(company_uuid,completed_by) REFERENCES public.staff_user(company_uuid,staff_user_uuid)
);

CREATE TABLE IF NOT EXISTS voc.retraining_candidate (
    ticket_uuid uuid REFERENCES voc.ticket_answer_completion,
    chat_completion_uuid uuid REFERENCES voc.chat_training_completion,
    channel text NOT NULL,
    company_uuid uuid NOT NULL REFERENCES public.company,
    domain_uuid uuid NOT NULL REFERENCES public.domain,
    input_uuid uuid NOT NULL,
    input_version integer NOT NULL CHECK(input_version>0),
    preprocess_version integer,
    kind text NOT NULL CHECK(kind IN ('sentiment','category')),
    value text NOT NULL CHECK(btrim(value)<>''),
    origin text NOT NULL CHECK(origin IN ('상담사 확인 후 유지','상담사 직접 수정·지정')),
    completed_by uuid NOT NULL,
    completed_at timestamptz NOT NULL,
    label_staff_uuid uuid NOT NULL,
    evidence_at timestamptz NOT NULL CHECK(evidence_at<=completed_at),
    ticket_attempt_uuid uuid REFERENCES voc.inference_attempt,
    chat_attempt_uuid uuid,
    predicted_value text,
    model_name text,
    model_version text,
    rollout text CHECK (rollout IN ('stable','canary')),
    UNIQUE NULLS NOT DISTINCT (ticket_uuid,chat_completion_uuid,kind),
    FOREIGN KEY(company_uuid,completed_by) REFERENCES public.staff_user(company_uuid,staff_user_uuid),
    FOREIGN KEY(company_uuid,label_staff_uuid) REFERENCES public.staff_user(company_uuid,staff_user_uuid),
    FOREIGN KEY(company_uuid,input_uuid,preprocess_version,input_version) REFERENCES voc.model_text_history,
    CHECK ((channel IN ('gmail','discord') AND ticket_uuid IS NOT NULL AND chat_completion_uuid IS NULL
            AND preprocess_version IS NOT NULL AND chat_attempt_uuid IS NULL)
           OR (channel='chat' AND chat_completion_uuid IS NOT NULL AND ticket_uuid IS NULL
               AND preprocess_version IS NULL AND ticket_attempt_uuid IS NULL)),
    CHECK (origin<>'상담사 확인 후 유지'
           OR (coalesce(ticket_attempt_uuid,chat_attempt_uuid) IS NOT NULL
               AND predicted_value IS NOT NULL AND predicted_value=value AND label_staff_uuid=completed_by))
);
CREATE INDEX IF NOT EXISTS retraining_candidate_company_completion
    ON voc.retraining_candidate(company_uuid,completed_at DESC,ticket_uuid,chat_completion_uuid,kind);

CREATE OR REPLACE FUNCTION voc.eligible_ticket_retraining_candidates(completion voc.ticket_answer_completion)
RETURNS SETOF voc.retraining_candidate LANGUAGE sql STABLE AS $$
    SELECT (completion).ticket_uuid,NULL::uuid,ch.type,i.company_uuid,i.domain_uuid,i.event_id,i.body_version,i.preprocess_version,
           f.kind,f.value,
           CASE WHEN d.displayed_at IS NOT NULL THEN '상담사 확인 후 유지' ELSE '상담사 직접 수정·지정' END,
           (completion).completed_by,(completion).completed_at,coalesce(d.staff_user_uuid,e.staff_user_uuid),
           coalesce(d.displayed_at,e.edited_at),p.attempt_id,NULL::uuid,p.value,p.model_name,p.model_version,p.rollout
    FROM voc.ticket_training_input i
    JOIN public.ticket t ON t.ticket_uuid=i.ticket_uuid
    JOIN public.channel ch ON ch.channel_uuid=t.channel_uuid AND ch.type IN ('gmail','discord')
    JOIN public.staff_user finisher ON finisher.staff_user_uuid=(completion).completed_by
        AND finisher.company_uuid=i.company_uuid
    JOIN public.ticket_message m ON m.ticket_message_uuid=(completion).delivery_uuid AND m.ticket_uuid=t.ticket_uuid
        AND m.staff_user_uuid=finisher.staff_user_uuid AND m.sender_type='staff' AND m.message_type='reply'
        AND m.delivery_status='delivered' AND m.created_at<=(completion).completed_at
    CROSS JOIN (VALUES ('sentiment',(completion).sentiment),('category',(completion).category_uuid::text)) f(kind,value)
    LEFT JOIN LATERAL (
        SELECT p.attempt_id,p.value,a.model_name,a.model_version,a.rollout,a.ended_at
        FROM voc.ticket_prediction p JOIN voc.inference_attempt a USING(attempt_id)
        WHERE p.ticket_uuid=i.ticket_uuid AND p.kind=f.kind AND a.ticket_uuid=i.ticket_uuid
          AND a.api='classification' AND a.traffic='operational' AND a.ended_at<=(completion).completed_at
          AND a.body_version=i.body_version
          AND (a.preprocess_version=i.preprocess_version OR (a.preprocess_version IS NULL AND NOT EXISTS (
              SELECT FROM voc.model_text_history h
              WHERE h.company_uuid=i.company_uuid AND h.event_id=i.event_id AND h.body_version=i.body_version
                AND h.preprocess_version<>i.preprocess_version
          )))
          AND nullif(btrim(a.model_name),'') IS NOT NULL AND nullif(btrim(a.model_version),'') IS NOT NULL
          AND a.rollout IS NOT NULL
    ) p ON true
    LEFT JOIN voc.prediction_display d ON d.ticket_uuid=i.ticket_uuid AND d.kind=f.kind
        AND d.staff_user_uuid=(completion).completed_by AND d.attempt_id=p.attempt_id
        AND d.displayed_value=p.value AND p.value=f.value
        AND d.displayed_at BETWEEN p.ended_at AND (completion).completed_at
    LEFT JOIN voc.ticket_classification_edit e ON e.ticket_uuid=i.ticket_uuid AND e.kind=f.kind
        AND e.value=f.value AND e.body_version=i.body_version AND e.preprocess_version=i.preprocess_version
        AND e.edited_at<=(completion).completed_at
        AND EXISTS (SELECT FROM public.staff_user st
                    WHERE st.staff_user_uuid=e.staff_user_uuid AND st.company_uuid=i.company_uuid)
    WHERE i.ticket_uuid=(completion).ticket_uuid AND i.available_at<=(completion).completed_at
      AND nullif(btrim(f.value),'') IS NOT NULL
      AND (d.displayed_at IS NOT NULL OR e.edited_at IS NOT NULL);
$$;

CREATE OR REPLACE FUNCTION voc.observe_answer_completion() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE completion voc.ticket_answer_completion%ROWTYPE;
BEGIN
    INSERT INTO voc.ticket_answer_completion(ticket_uuid,category_uuid,sentiment,completed_by,delivery_uuid)
    SELECT NEW.ticket_uuid,NEW.category_uuid,NEW.sentiment,m.staff_user_uuid,m.ticket_message_uuid
    FROM public.ticket_message m JOIN public.staff_user st ON st.staff_user_uuid=m.staff_user_uuid
    WHERE m.ticket_message_uuid=NEW.reply_delivery_uuid AND m.ticket_uuid=NEW.ticket_uuid
      AND st.company_uuid=NEW.company_uuid AND m.sender_type='staff' AND m.message_type='reply'
      AND m.delivery_status='delivered'
    ON CONFLICT DO NOTHING
    RETURNING * INTO completion;
    IF NOT FOUND THEN
        RETURN NEW;
    END IF;

    INSERT INTO voc.retraining_candidate
    SELECT * FROM voc.eligible_ticket_retraining_candidates(completion)
    ON CONFLICT DO NOTHING;
    RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER monitor_answer_completion
    AFTER UPDATE OF status ON public.ticket FOR EACH ROW
    WHEN (NEW.status='답변완료' AND OLD.status IS DISTINCT FROM NEW.status)
    EXECUTE FUNCTION voc.observe_answer_completion();

CREATE OR REPLACE FUNCTION voc.monitor_ticket_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM voc.observe_stage(NEW.source_event_id,'ticket',clock_timestamp(),clock_timestamp());
    PERFORM voc.recover_stage('ticket',NEW.source_event_id);
    RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER monitor_ticket_insert AFTER INSERT ON public.ticket
    FOR EACH ROW EXECUTE FUNCTION voc.monitor_ticket_insert();

CREATE OR REPLACE FUNCTION voc.schedule_embedding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.analysis_kind='sentiment' THEN
        INSERT INTO voc.ticket_analysis_outbox(company_uuid,event_id,preprocess_version,body_version,analysis_kind)
        VALUES (NEW.company_uuid,NEW.event_id,NEW.preprocess_version,NEW.body_version,'embedding')
        ON CONFLICT DO NOTHING;
    END IF;
    RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER schedule_ticket_embedding AFTER INSERT ON voc.ticket_analysis_outbox
    FOR EACH ROW EXECUTE FUNCTION voc.schedule_embedding();

-- Queue old tickets using the existing input. Historical stage timestamps cannot be reconstructed.
INSERT INTO voc.ticket_analysis_outbox(company_uuid,event_id,preprocess_version,body_version,analysis_kind)
SELECT h.company_uuid,h.event_id,h.preprocess_version,h.body_version,'embedding'
FROM voc.model_text_history h JOIN public.ticket t ON t.source_event_id=h.event_id
JOIN voc.silver_messages s ON s.event_id=h.event_id AND s.schema_version=4 AND s.body_version=h.body_version
WHERE s.message_kind='customer_inquiry' AND t.embedding IS NULL
ON CONFLICT DO NOTHING;

CREATE OR REPLACE VIEW public.visible_ticket AS
SELECT t.* FROM public.ticket t JOIN voc.silver_messages s ON s.event_id=t.source_event_id AND s.schema_version=4
WHERE s.message_kind='customer_inquiry'
  AND (t.classification_final_at IS NOT NULL OR (t.sentiment_status='succeeded' AND t.category_status='succeeded'));

CREATE TABLE IF NOT EXISTS voc.evaluation_set (
    dataset_version text PRIMARY KEY, checksum text NOT NULL,
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS voc.evaluation_label (
    dataset_version text NOT NULL REFERENCES voc.evaluation_set,
    label_id text NOT NULL, sample_id text NOT NULL,
    kind text NOT NULL CHECK(kind IN ('sentiment','category')),
    expected text NOT NULL, reviewer_ref text NOT NULL, review_evidence text NOT NULL,
    PRIMARY KEY(dataset_version,label_id),
    UNIQUE(dataset_version,sample_id,kind)
);
CREATE TABLE IF NOT EXISTS voc.evaluation_result (
    run_id uuid NOT NULL, dataset_version text NOT NULL REFERENCES voc.evaluation_set,
    rollout text NOT NULL CHECK(rollout IN ('stable','canary')),
    model_name text NOT NULL, model_version text NOT NULL, kind text NOT NULL CHECK(kind IN ('sentiment','category')),
    macro_f1 double precision NOT NULL CHECK(macro_f1 BETWEEN 0 AND 1),
    samples integer NOT NULL CHECK(samples>0), evaluated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(run_id,rollout,kind)
);

-- This reporting schema deliberately omits all message bodies and customer account details.
CREATE OR REPLACE VIEW monitor.stage AS SELECT * FROM voc.pipeline_stage;
CREATE OR REPLACE VIEW monitor.kafka_delivery AS SELECT * FROM voc.pipeline_kafka;
CREATE OR REPLACE VIEW monitor.flink_attempt AS SELECT * FROM voc.flink_attempt;
CREATE OR REPLACE VIEW monitor.failure AS SELECT * FROM voc.pipeline_failure;
CREATE OR REPLACE VIEW monitor.inference AS
SELECT api,traffic,started_at,ended_at,model_name,model_version,rollout,preferred_route,http_status,response_ms,model_ms,error
FROM voc.inference_attempt;
CREATE OR REPLACE VIEW monitor.evaluation AS SELECT * FROM voc.evaluation_result;
CREATE OR REPLACE VIEW monitor.modification AS
SELECT c.completed_at,p.kind,p.displayed_value,
       CASE p.kind WHEN 'category' THEN c.category_uuid::text ELSE c.sentiment END AS final_value,
       a.rollout,a.model_name,a.model_version
FROM voc.ticket_answer_completion c JOIN voc.prediction_display p USING(ticket_uuid)
JOIN voc.inference_attempt a USING(attempt_id)
WHERE p.staff_user_uuid=c.completed_by AND p.displayed_at<=c.completed_at AND a.traffic='operational';
CREATE OR REPLACE VIEW monitor.answer_completion AS SELECT completed_at FROM voc.ticket_answer_completion;

CREATE OR REPLACE VIEW monitor.queue_backlog AS
SELECT 'flink' AS stage,count(*) AS pending,extract(epoch FROM clock_timestamp()-min(queued_at))::float8 AS oldest_seconds
FROM voc.standardize_outbox WHERE published_at IS NULL
UNION ALL
SELECT CASE analysis_kind WHEN 'sentiment' THEN 'classification' ELSE analysis_kind END,
       count(*),extract(epoch FROM clock_timestamp()-min(q.queued_at))::float8
FROM voc.ticket_analysis_outbox q JOIN voc.silver_messages s
  ON s.event_id=q.event_id AND s.schema_version=4 AND s.body_version=q.body_version
WHERE s.message_kind='customer_inquiry' AND analysis_kind IN ('sentiment','embedding') AND finished_at IS NULL
GROUP BY analysis_kind;

CREATE OR REPLACE VIEW monitor.source_reconciliation AS
WITH tickets AS (
    SELECT t.source_event_id,count(DISTINCT t.ticket_uuid) AS ticket_count,min(t.ready_at) AS ready_at
    FROM public.ticket t JOIN public.channel c USING(channel_uuid)
    WHERE c.type IN ('gmail','discord') GROUP BY t.source_event_id
), sources AS (
    SELECT b.event_id,b.channel,b.received_at,o.published_at,o.event_id IS NOT NULL AS has_outbox,
           coalesce(nullif(s.message_kind,'unknown'),o.verified_message_kind,'unknown') AS message_kind,
           s.message_kind='customer_inquiry' AS has_confirmed_silver,s.event_id IS NOT NULL AS has_silver,
           p.event_id IS NOT NULL AS has_projection_error
    FROM voc.bronze_messages b
    LEFT JOIN voc.silver_messages s ON s.event_id=b.event_id AND s.schema_version=4
    LEFT JOIN voc.standardize_outbox o ON o.event_id=b.event_id AND o.target_schema_version=4
    LEFT JOIN voc.ticket_projection_issue p ON p.event_id=b.event_id
)
SELECT coalesce(s.event_id,t.source_event_id) AS event_id,s.channel,s.received_at,t.ready_at,
       coalesce(t.ticket_count,0) AS ticket_count,
       greatest(coalesce(t.ticket_count,0)-1,0) AS extra_tickets,
       CASE WHEN s.event_id IS NULL THEN 'ticket_without_source'
            WHEN s.message_kind IN ('staff_reply','other') THEN 'not_customer'
            WHEN t.ticket_count>0 THEN 'matched'
            WHEN s.message_kind<>'customer_inquiry' THEN 'unconfirmed'
            WHEN s.has_projection_error OR EXISTS (
                SELECT FROM voc.pipeline_failure f WHERE f.event_id=s.event_id AND f.recovered_at IS NULL
                      AND f.stage IN ('bronze','flink','ticket'))
                 OR (NOT s.has_silver AND EXISTS (
                      SELECT FROM voc.review_events e WHERE e.event_id=s.event_id::text
                        AND e.topic IN ('voc.standardize.review.v4','voc.standardized.v4')
                 )) THEN 'failed'
            WHEN s.has_outbox AND s.published_at IS NULL THEN 'pending'
            WHEN s.has_confirmed_silver OR EXISTS (
                SELECT FROM voc.pipeline_stage p WHERE p.event_id=s.event_id
                    AND p.stage='ticket' AND p.succeeded_at IS NOT NULL
            ) OR EXISTS (
                SELECT FROM voc.pipeline_kafka k WHERE k.event_id=s.event_id AND k.topic='voc.standardized.v4'
            ) THEN 'source_without_ticket'
            WHEN EXISTS (
                SELECT FROM voc.pipeline_kafka k JOIN voc.kafka_progress p USING(topic,partition_id)
                WHERE k.event_id=s.event_id AND k.topic='voc.standardize.request.v4'
                  AND p.consumer_group='encore-flink-standardize-v4'
                  AND p.observed_at>=clock_timestamp()-interval '45 seconds'
                  AND p.high_watermark>k.kafka_offset AND coalesce(p.committed_offset,0)<=k.kafka_offset
            ) THEN 'pending'
            ELSE 'unconfirmed_delivery' END AS reconciliation
FROM sources s FULL JOIN tickets t ON s.event_id=t.source_event_id;

CREATE OR REPLACE FUNCTION voc.source_registered(channel text, raw jsonb) RETURNS timestamptz
LANGUAGE sql STABLE AS $$
    SELECT CASE
      WHEN channel='gmail' AND (raw->>'internalDate') ~ '^[0-9]{1,13}$'
           THEN to_timestamp((raw->>'internalDate')::double precision / 1000)
      WHEN channel='discord' AND coalesce(raw#>>'{d,timestamp}',raw->>'timestamp') ~ '^[0-9]{4}-'
           AND pg_input_is_valid(coalesce(raw#>>'{d,timestamp}',raw->>'timestamp'),'timestamptz')
           THEN coalesce(raw#>>'{d,timestamp}',raw->>'timestamp')::timestamptz
    END
$$;
CREATE OR REPLACE VIEW monitor.normal_inquiry AS
SELECT b.event_id,coalesce(s.occurred_at,voc.source_registered(b.channel,b.raw_payload)) AS registered_at,t.ready_at
FROM voc.bronze_messages b JOIN voc.standardize_outbox o ON o.event_id=b.event_id AND o.target_schema_version=4
LEFT JOIN voc.silver_messages s ON s.event_id=b.event_id AND s.schema_version=4
LEFT JOIN public.ticket t ON t.source_event_id=b.event_id
WHERE coalesce(nullif(s.message_kind,'unknown'),o.verified_message_kind)='customer_inquiry';

CREATE OR REPLACE VIEW monitor.projection_issue AS
SELECT event_id,reason,recorded_at FROM voc.ticket_projection_issue;

CREATE OR REPLACE FUNCTION voc.monitor_ticket_arrival() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.message_kind='customer_inquiry' THEN
        PERFORM voc.observe_stage(NEW.event_id,'ticket',clock_timestamp(),NULL);
    END IF;
    RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER monitor_ticket_arrival BEFORE INSERT OR UPDATE OF message_kind ON voc.silver_messages
    FOR EACH ROW EXECUTE FUNCTION voc.monitor_ticket_arrival();

CREATE OR REPLACE FUNCTION voc.monitor_projection_failure() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM voc.fail_stage('ticket',NEW.event_id::text || ':' || NEW.reason,NEW.event_id,NEW.reason);
    RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER monitor_projection_failure AFTER INSERT OR UPDATE ON voc.ticket_projection_issue
    FOR EACH ROW EXECUTE FUNCTION voc.monitor_projection_failure();

CREATE OR REPLACE VIEW monitor.analysis_retries AS
SELECT t.source_event_id AS event_id,a.api AS stage,greatest(count(*)-1,0) AS retries,max(a.started_at) AS last_at
FROM voc.inference_attempt a JOIN public.ticket t USING(ticket_uuid)
WHERE a.traffic='operational' GROUP BY t.source_event_id,a.api HAVING count(*)>1;

CREATE TABLE IF NOT EXISTS voc.pipeline_availability (
    step text NOT NULL, bucket timestamptz NOT NULL, seen_from timestamptz NOT NULL, seen_until timestamptz NOT NULL,
    PRIMARY KEY(step,bucket)
);
CREATE OR REPLACE FUNCTION voc.heartbeat_pipeline(name text) RETURNS void LANGUAGE sql AS $$
    INSERT INTO voc.pipeline_availability(step,bucket,seen_from,seen_until)
    SELECT name,date_trunc('minute',seen),seen,seen FROM (SELECT clock_timestamp() AS seen) tick
    ON CONFLICT(step,bucket) DO UPDATE SET seen_until=greatest(voc.pipeline_availability.seen_until,excluded.seen_until)
$$;
CREATE OR REPLACE VIEW monitor.availability AS SELECT step,seen_from,seen_until FROM voc.pipeline_availability;

CREATE TABLE IF NOT EXISTS voc.collector_position (
    channel text NOT NULL, connection_hash text NOT NULL, position text NOT NULL, phase text NOT NULL,
    updated_at timestamptz, observed_at timestamptz NOT NULL,
    PRIMARY KEY(channel,connection_hash)
);
CREATE OR REPLACE VIEW monitor.collector_position AS
SELECT channel,connection_hash,position,phase,updated_at
FROM voc.collector_position WHERE observed_at >= clock_timestamp()-interval '2 minutes';

CREATE TABLE IF NOT EXISTS voc.reply_dispatch (
    reply_uuid uuid PRIMARY KEY, ticket_uuid uuid NOT NULL REFERENCES public.ticket,
    staff_user_uuid uuid NOT NULL REFERENCES public.staff_user, body text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    claim_token uuid, lease_until timestamptz, provider_delivery_id text, delivered_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS pending_ticket_reply ON voc.reply_dispatch(ticket_uuid) WHERE delivered_at IS NULL;
