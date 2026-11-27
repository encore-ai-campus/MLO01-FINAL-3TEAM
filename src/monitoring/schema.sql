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

CREATE OR REPLACE VIEW voc_monitor.queue_backlog AS
SELECT 'classification'::text AS stage,count(*) FILTER (WHERE published_at IS NULL) AS pending,
       extract(epoch FROM (statement_timestamp()-min(queued_at) FILTER (WHERE published_at IS NULL))) AS oldest_seconds
FROM voc.ticket_analysis_outbox;

CREATE OR REPLACE VIEW voc_monitor.inventory AS
SELECT (SELECT count(*) FROM voc.bronze_messages) AS bronze,
       (SELECT count(*) FROM public.ticket WHERE source_event_id IS NOT NULL) AS tickets,
       (SELECT pending FROM voc_monitor.queue_backlog WHERE stage='classification') AS queued;

-- A Kafka publish is observable before Silver stores the same request version.
CREATE OR REPLACE VIEW voc_monitor.standardization AS
SELECT o.event_id,o.queued_at,o.published_at,s.standardized_at
FROM voc.standardize_outbox o LEFT JOIN voc.silver_messages s
  ON (s.event_id,s.schema_version,s.body_version)=(o.event_id,o.target_schema_version,o.body_version)
WHERE o.target_schema_version=4;

CREATE OR REPLACE VIEW voc_monitor.standardized_inquiries AS
SELECT standardized_at
FROM voc.silver_messages
WHERE schema_version=4 AND message_kind='customer_inquiry';

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

-- Collection-only installations can start before the classification worker schema.
SELECT to_regclass('voc.classification_attempt') IS NOT NULL AS has_attempts \gset
\if :has_attempts
CREATE OR REPLACE VIEW voc_monitor.inference AS
SELECT 'classification'::text AS api,
       CASE WHEN b.context->>'synthetic'='true' OR b.origin='synthetic-canary'
            THEN 'replay' ELSE 'operational' END AS traffic,
       coalesce(to_jsonb(a)->>'assigned_route',j.route) AS rollout,
       CASE WHEN a.served_version IN ('klue-v1','klue-v2') THEN 'KLUE-BERT' ELSE a.served_version END AS model_name,
       a.served_version AS model_version,a.started_at,a.finished_at AS ended_at,
       (to_jsonb(a)->>'http_ms')::double precision AS response_ms,NULL::double precision AS model_ms,
       CASE WHEN a.outcome IN ('failed','expired') THEN coalesce(a.error_code,a.outcome) END AS error,
       j.analysis_kind,t.ticket_number,
       CASE WHEN a.outcome='succeeded' AND a.attempt_no=j.attempt_count AND j.state='succeeded' THEN
           CASE WHEN j.analysis_kind='sentiment' THEN j.label ELSE c.name END
       END AS prediction,
       (a.outcome='succeeded' AND a.attempt_no=j.attempt_count AND j.applied) AS applied,
       j.company_uuid, CASE WHEN j.analysis_kind='sentiment' THEN 'sentiment' ELSE 'category/'||t.domain_uuid END AS scope_key,
       j.event_id,coalesce(to_jsonb(a)->>'assigned_version',j.model_version) AS assigned_version,
       (to_jsonb(a)->>'cost_krw')::numeric AS cost_krw
FROM voc.classification_attempt a JOIN voc.classification_job j USING(request_id)
JOIN voc.bronze_messages b ON b.event_id=j.event_id
LEFT JOIN public.ticket t ON (t.company_uuid,t.source_event_id)=(j.company_uuid,j.event_id)
LEFT JOIN voc.classification_category_map m ON j.analysis_kind='category' AND (m.domain_uuid,m.label)=(t.domain_uuid,j.label)
LEFT JOIN public.category c ON (c.domain_uuid,c.category_uuid)=(m.domain_uuid,m.category_uuid);
\else
CREATE OR REPLACE VIEW voc_monitor.inference AS
SELECT NULL::text AS api,NULL::text AS traffic,NULL::text AS rollout,NULL::text AS model_name,
       NULL::text AS model_version,NULL::timestamptz AS started_at,NULL::timestamptz AS ended_at,
       NULL::double precision AS response_ms,NULL::double precision AS model_ms,NULL::text AS error,
       NULL::text AS analysis_kind,NULL::text AS ticket_number,NULL::text AS prediction,NULL::boolean AS applied,
       NULL::uuid AS company_uuid,NULL::text AS scope_key,NULL::uuid AS event_id,NULL::text AS assigned_version,NULL::numeric AS cost_krw
WHERE false;
\endif

SELECT to_regclass('voc.classification_rollout') IS NOT NULL AS has_rollout,
       to_regclass('voc.model_review_policy') IS NOT NULL AS has_policy \gset
\if :has_rollout
CREATE OR REPLACE VIEW voc_monitor.rollout AS
SELECT company_uuid,scope_key,stable_version,candidate_version,configured_percent,
       CASE WHEN tripped_at IS NULL THEN configured_percent ELSE 0 END AS effective_percent,
       (SELECT count(*) FROM jsonb_object_keys(failures))::int AS distinct_failures,
       first_failure_at,tripped_at,reason,
       round(extract(epoch FROM tripped_at-first_failure_at)*1000,1) AS recovery_ms
FROM voc.classification_rollout;
\else
CREATE OR REPLACE VIEW voc_monitor.rollout AS SELECT NULL::uuid AS company_uuid,NULL::text AS scope_key,
       NULL::text AS stable_version,NULL::text AS candidate_version,NULL::smallint AS configured_percent,
       NULL::integer AS effective_percent,NULL::integer AS distinct_failures,NULL::timestamptz AS first_failure_at,
       NULL::timestamptz AS tripped_at,NULL::text AS reason,NULL::numeric AS recovery_ms WHERE false;
\endif
\if :has_policy
CREATE OR REPLACE VIEW voc_monitor.review_policy AS
SELECT p.company_uuid,c.name AS company_name,p.scope_key,p.revision,p.settings,p.updated_at
FROM voc.model_review_policy p JOIN public.company c USING(company_uuid);
\else
CREATE OR REPLACE VIEW voc_monitor.review_policy AS SELECT NULL::uuid AS company_uuid,NULL::text AS company_name,
       NULL::text AS scope_key,NULL::integer AS revision,NULL::jsonb AS settings,NULL::timestamptz AS updated_at WHERE false;
\endif
CREATE OR REPLACE VIEW voc_monitor.review_scopes AS
SELECT s.company_uuid,coalesce(c.name,s.company_uuid::text) AS company_name,s.scope_key,
       CASE WHEN scope_key='sentiment' THEN '공통 감성' ELSE coalesce(d.name,'회사 도메인')||' 세부유형' END AS scope_name
FROM (SELECT company_uuid,scope_key FROM voc_monitor.review_policy
      UNION SELECT company_uuid,scope_key FROM voc_monitor.rollout) s
JOIN public.company c USING(company_uuid)
LEFT JOIN public.domain d ON s.scope_key='category/'||d.domain_uuid;

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
