\getenv reader_password MONITOR_READER_PASSWORD
BEGIN;
SELECT pg_advisory_xact_lock(hashtext('voc-monitoring-install'));
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='voc_monitor_reader') THEN
        CREATE ROLE voc_monitor_reader LOGIN NOINHERIT;
    END IF;
END $$;
SELECT format('ALTER ROLE voc_monitor_reader PASSWORD %L', :'reader_password') \gexec
ALTER ROLE voc_monitor_reader SET default_transaction_read_only=on;
ALTER ROLE voc_monitor_reader SET statement_timeout='5s';

CREATE SCHEMA IF NOT EXISTS voc_monitor;
REVOKE ALL ON SCHEMA voc_monitor FROM PUBLIC;

-- Reconcile current records. CSV imports have no collector event and are outside this comparison.
CREATE OR REPLACE VIEW voc_monitor.source_reconciliation AS
WITH tickets AS (
    SELECT source_event_id,count(*) AS ticket_count
    FROM public.ticket WHERE source_event_id IS NOT NULL GROUP BY source_event_id
)
SELECT coalesce(b.event_id,t.source_event_id) AS event_id,b.channel,b.received_at,
       coalesce(t.ticket_count,0) AS ticket_count,greatest(coalesce(t.ticket_count,0)-1,0) AS extra_tickets,
       CASE WHEN b.event_id IS NULL THEN 'ticket_without_source'
            WHEN p.event_id IS NOT NULL AND t.source_event_id IS NULL THEN 'projection_issue'
            WHEN o.published_at IS NULL THEN 'pending'
            WHEN s.event_id IS NULL OR s.body_version<>o.body_version THEN 'unconfirmed_delivery'
            WHEN s.message_kind<>'customer_inquiry' THEN 'not_inquiry'
            WHEN t.source_event_id IS NULL THEN 'source_without_ticket'
            ELSE 'matched' END AS reconciliation,
       p.event_id IS NOT NULL AS has_projection_issue
FROM voc.bronze_messages b FULL JOIN tickets t ON t.source_event_id=b.event_id
LEFT JOIN voc.standardize_outbox o ON o.event_id=b.event_id AND o.target_schema_version=4
LEFT JOIN voc.silver_messages s ON s.event_id=b.event_id AND s.schema_version=4
LEFT JOIN voc.ticket_projection_issue p ON p.event_id=b.event_id;

CREATE OR REPLACE VIEW voc_monitor.inventory AS
SELECT (SELECT count(*) FROM voc.bronze_messages) AS bronze,
       (SELECT count(*) FROM public.ticket WHERE source_event_id IS NOT NULL) AS tickets,
       (SELECT CASE WHEN count(*)>0 THEN count(*) FILTER (WHERE published_at IS NULL) END
        FROM voc.ticket_analysis_outbox) AS queued;

-- The interval includes Kafka delivery/consumption and DB storage. It is not Flink-only latency.
CREATE OR REPLACE VIEW voc_monitor.standardization AS
SELECT s.event_id,o.queued_at,o.published_at,s.standardized_at
FROM voc.silver_messages s JOIN voc.standardize_outbox o
  ON (s.event_id,s.schema_version,s.body_version)=(o.event_id,o.target_schema_version,o.body_version);

CREATE OR REPLACE VIEW voc_monitor.diagnostics AS
SELECT 'capture'::text AS stage,b.received_at AS observed_at,'원문에 기록된 수집 시각'::text AS activity,
       b.event_id::text AS event_id,NULL::text AS topic,NULL::integer AS partition_id,
       NULL::bigint AS kafka_offset,NULL::text AS reason
FROM voc.bronze_messages b
UNION ALL
SELECT 'bronze',o.queued_at,'정제 요청 대기 등록',o.event_id::text,NULL,NULL,NULL,NULL
FROM voc.standardize_outbox o WHERE o.target_schema_version=4
UNION ALL
SELECT 'bronze',o.published_at,'정제 요청 Kafka 전달 확인',o.event_id::text,'voc.standardize.request.v4',NULL,NULL,NULL
FROM voc.standardize_outbox o WHERE o.target_schema_version=4 AND o.published_at IS NOT NULL
UNION ALL
SELECT 'flink',s.standardized_at,'Silver DB 저장',s.event_id::text,NULL,NULL,NULL,NULL
FROM voc.silver_messages s WHERE s.schema_version=4
UNION ALL
SELECT CASE WHEN r.topic IN ('voc.raw.discord','voc.raw.gmail') THEN 'bronze' ELSE 'flink' END,
       r.recorded_at,'검토 기록',
       CASE WHEN r.event_id ~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' THEN r.event_id END,
       r.topic,r.partition_id,r.kafka_offset,
       CASE WHEN r.reason IN ('Discord scope mismatch','empty text body','invalid Discord event','invalid Discord reply',
                             'invalid body_version','invalid event_id','invalid message kind','invalid timestamp',
                             'source ID mismatch','unsupported channel','invalid source format',
                             'source body differs from first Bronze record') THEN r.reason
            ELSE '입력 검토 필요' END
FROM voc.review_events r WHERE r.schema_version=4
  AND r.topic IN ('voc.raw.discord','voc.raw.gmail','voc.standardized.v4','voc.standardize.review.v4')
UNION ALL
SELECT 'ticket',p.recorded_at,'티켓 생성 보류',p.event_id::text,NULL,NULL,NULL,
       CASE WHEN p.reason IN ('unregistered_source','missing_company_domain','missing_sender') THEN p.reason
            ELSE '티켓 매핑 검토 필요' END
FROM voc.ticket_projection_issue p
UNION ALL
SELECT 'compute',o.queued_at,CASE o.analysis_kind WHEN 'sentiment' THEN '감성 분류 대기 등록'
                                                ELSE '카테고리 분류 대기 등록' END,
       o.event_id::text,NULL,NULL,NULL,NULL
FROM voc.ticket_analysis_outbox o;

CREATE OR REPLACE VIEW voc_monitor.queue_backlog AS
SELECT 'classification'::text AS stage,count(*) FILTER (WHERE published_at IS NULL) AS pending,
       extract(epoch FROM (statement_timestamp()-min(queued_at) FILTER (WHERE published_at IS NULL))) AS oldest_seconds
FROM voc.ticket_analysis_outbox HAVING count(*)>0;

-- These views describe the data a future inference/quality producer needs to supply.
CREATE OR REPLACE VIEW voc_monitor.inference AS
SELECT NULL::text AS api,NULL::text AS traffic,NULL::text AS rollout,NULL::text AS model_name,
       NULL::text AS model_version,NULL::timestamptz AS started_at,NULL::timestamptz AS ended_at,
       NULL::double precision AS response_ms,NULL::double precision AS model_ms,NULL::text AS error
WHERE false;

CREATE OR REPLACE VIEW voc_monitor.evaluation AS
SELECT NULL::uuid AS run_id,NULL::text AS rollout,NULL::text AS model_name,NULL::text AS model_version,
       NULL::text AS dataset_version,NULL::text AS kind,NULL::double precision AS macro_f1,
       NULL::integer AS samples,NULL::timestamptz AS evaluated_at WHERE false;

CREATE OR REPLACE VIEW voc_monitor.modification AS
SELECT NULL::text AS rollout,NULL::text AS model_name,NULL::text AS model_version,NULL::text AS kind,
       NULL::text AS displayed_value,NULL::text AS final_value,NULL::timestamptz AS completed_at WHERE false;

CREATE OR REPLACE VIEW voc_monitor.answer_completion AS
SELECT NULL::timestamptz AS completed_at WHERE false;

GRANT USAGE ON SCHEMA voc_monitor TO voc_monitor_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA voc_monitor TO voc_monitor_reader;
COMMIT;
