-- Use real Silver-to-ticket triggers; only the source_channel_type input is synthetic.
\if :{?body}
\else
\set body '시험 주문의 배송 상태를 알려주세요.'
\endif
WITH inputs AS (
    INSERT INTO voc.bronze_message(event_id, source_company_id, source_channel_type, source_connection_id, source_message_id, origin,
                                   received_at, context_json, raw_payload_json)
    SELECT id, 'canary-demo','gmail','demo-inbox', :'batch' || '/' || id::text,
           'synthetic-canary',clock_timestamp(),'{}','{}'
    FROM unnest(:'ids'::uuid[]) AS t(id)
    RETURNING event_id, source_company_id, source_channel_type, source_connection_id, source_message_id, received_at
)
INSERT INTO voc.silver_message(schema_version, event_id, source_company_id, source_channel_type, source_connection_id, source_message_id,
                               source_conversation_id, message_kind, sender_ref, title,
                               body_text, body_version, received_at, source_metadata_json)
SELECT 4,event_id,source_company_id,source_channel_type,source_connection_id,source_message_id,source_message_id,
       'customer_inquiry','synthetic@example.test','시연용 배송 문의',:'body',1,
       received_at,'{"label_ids":[],"to_addresses":[]}'
FROM inputs;
