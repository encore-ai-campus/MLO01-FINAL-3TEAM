-- Run on a disposable database after schema.sql, ticket_schema.sql and channel_intake_schema.sql.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL plpgsql.check_asserts=on;
CREATE TEMP TABLE cases (
    id text PRIMARY KEY, expected boolean DEFAULT false, event uuid DEFAULT gen_random_uuid(),
    company_id text DEFAULT 'intake-test', channel text DEFAULT 'gmail', connection_id text DEFAULT 'desk@example.test',
    room text, origin text DEFAULT 'gmail_api', context jsonb DEFAULT '{"label":"VOC"}',
    kind text DEFAULT 'unknown', confirmed text, sender text DEFAULT 'client@example.test',
    labels jsonb DEFAULT '["Label_42"]', received_at timestamptz DEFAULT '2026-11-02T00:00:00Z'
);
INSERT INTO cases(id,expected) VALUES('first',true),('during activation',true);
INSERT INTO cases(id,channel,connection_id,room,origin,context,expected) VALUES
    ('room','discord','guild','support','discord_gateway','{"channel_id":"support"}',true),
    ('history','discord','guild','support','discord_history','{"channel_id":"support"}',true),
    ('other room','discord','guild','other','discord_gateway','{"channel_id":"other"}',false),
    ('wrong provider room','discord','guild','support','discord_gateway','{"channel_id":"other"}',false);
INSERT INTO cases(id,kind) VALUES('bot or automatic','other'),('outbound','staff_reply');
INSERT INTO cases(id,confirmed) VALUES('manual unknown','unknown'),('manual other','other');
INSERT INTO cases(id,labels) VALUES('spam','["SPAM"]'),('trash','["TRASH"]');
INSERT INTO cases(id,context) VALUES('other label','{"label":"Other"}');
INSERT INTO cases(id,origin) VALUES('not provider','test-import');
INSERT INTO cases(id,sender) VALUES('no sender',NULL);
INSERT INTO cases(id,received_at) VALUES('before activation','2026-10-31T23:59:59Z');
INSERT INTO cases(id,company_id) VALUES('other tenant','elsewhere');
INSERT INTO cases(id,connection_id) VALUES('other mailbox','another@example.test');

INSERT INTO public.domain(name) VALUES('intake-test') RETURNING domain_uuid \gset
INSERT INTO public.company(email_domain,domain_uuid) VALUES('intake.invalid',:'domain_uuid') RETURNING company_uuid \gset
INSERT INTO public.channel(company_uuid,name,type) VALUES(:'company_uuid','mail','gmail'),(:'company_uuid','room','discord');
INSERT INTO voc.ticket_source_connection(company_id,channel,connection_id,source_channel_id,company_uuid,channel_uuid)
    SELECT 'intake-test',type,CASE type WHEN 'gmail' THEN 'desk@example.test' ELSE 'guild' END,
        CASE type WHEN 'gmail' THEN '' ELSE 'support' END,company_uuid,channel_uuid
    FROM public.channel WHERE company_uuid=:'company_uuid';

INSERT INTO voc.bronze_messages(event_id,company_id,channel,connection_id,source_message_id,origin,received_at,context,raw_payload)
    SELECT event,company_id,channel,connection_id,id,origin,received_at,context,'{}' FROM cases;
INSERT INTO voc.standardize_outbox(event_id,verified_message_kind,published_at)
    SELECT event,confirmed,now() FROM cases;
CREATE TEMP VIEW normalized AS SELECT 4 AS schema_version,event AS event_id,company_id,channel,connection_id,
    id AS source_message_id,id AS source_conversation_id,room AS source_channel_id,kind AS message_kind,
    sender AS sender_ref,'문의'::text AS body_text,1 AS body_version,received_at,
    CASE channel WHEN 'discord' THEN '{}'::jsonb ELSE jsonb_build_object('label_ids',labels,'to_addresses','[]'::jsonb) END AS source_metadata
    FROM cases;
INSERT INTO voc.silver_messages(schema_version,event_id,company_id,channel,connection_id,source_message_id,
        source_conversation_id,source_channel_id,message_kind,sender_ref,body_text,body_version,received_at,source_metadata)
    SELECT * FROM normalized WHERE source_message_id IN ('before activation','during activation');
DO $$ BEGIN
    ASSERT NOT EXISTS (SELECT FROM voc.standardize_outbox WHERE auto_customer_confirmed_at IS NOT NULL);
END $$;
UPDATE voc.ticket_source_connection SET auto_customer_from='2026-11-01T00:00:00Z' WHERE company_uuid=:'company_uuid';
INSERT INTO voc.silver_messages(schema_version,event_id,company_id,channel,connection_id,source_message_id,
        source_conversation_id,source_channel_id,message_kind,sender_ref,body_text,body_version,received_at,source_metadata)
    SELECT * FROM normalized WHERE source_message_id NOT IN ('before activation','during activation');

DO $$ DECLARE mismatch text; selected uuid; original_stamp timestamptz; BEGIN
    SELECT string_agg(c.id,', ') INTO mismatch FROM cases c JOIN voc.standardize_outbox o ON c.event=o.event_id
    WHERE c.expected<>(o.auto_customer_confirmed_at IS NOT NULL)
       OR o.verified_message_kind IS DISTINCT FROM CASE WHEN c.expected THEN 'customer_inquiry' ELSE c.confirmed END
       OR c.expected<>(o.published_at IS NULL);
    ASSERT mismatch IS NULL, mismatch;
    SELECT c.event, o.auto_customer_confirmed_at INTO STRICT selected,original_stamp
        FROM cases c JOIN voc.standardize_outbox o ON c.event=o.event_id WHERE c.id='first';
    -- Once a human withdraws a route-derived decision, a catch-up/replay must leave it withdrawn.
    UPDATE voc.standardize_outbox SET verified_message_kind=NULL WHERE event_id=selected;
    PERFORM voc.confirm_channel_intake(selected);
    ASSERT (SELECT verified_message_kind IS NULL AND auto_customer_confirmed_at=original_stamp
            FROM voc.standardize_outbox WHERE event_id=selected);
END $$;
ROLLBACK;
