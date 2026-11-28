-- Run in an empty disposable database. The fixture and its migration are rolled back.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL plpgsql.check_asserts=on;
CREATE SCHEMA voc;
CREATE TABLE public.company (company_uuid uuid PRIMARY KEY);
CREATE TABLE voc.ticket_source_connection (
    company_id text PRIMARY KEY, company_uuid uuid REFERENCES public.company
);
CREATE TABLE voc.bronze_messages (event_id uuid PRIMARY KEY, company_id text, raw_payload jsonb);
CREATE TABLE voc.silver_messages (event_id uuid PRIMARY KEY REFERENCES voc.bronze_messages);
CREATE TABLE voc.feature_snapshot (feature_uuid uuid PRIMARY KEY, avg_first_reply_7d_seconds double precision);
CREATE TABLE voc.staff_assignment_policy (company_uuid uuid, staff_user_uuid uuid, enabled boolean);
CREATE TABLE public.staff_assignment_policy (LIKE voc.staff_assignment_policy);
CREATE TABLE voc.quick_reply (
    reply_uuid uuid, company_uuid uuid, owner_uuid uuid, team_uuid uuid, domain_uuid uuid, category_uuid uuid, body text
);
CREATE TABLE voc.messenger_destination (company_uuid uuid, team_uuid uuid, staff_user_uuid uuid, channel varchar(100));
INSERT INTO public.company VALUES ('00000000-0000-4000-8000-000000000001');
INSERT INTO voc.ticket_source_connection VALUES ('source-a', '00000000-0000-4000-8000-000000000001');
INSERT INTO voc.bronze_messages
VALUES ('00000000-0000-4000-8000-000000000002', 'source-a', '{"message":"original"}');
INSERT INTO voc.silver_messages SELECT event_id FROM voc.bronze_messages;
INSERT INTO voc.feature_snapshot VALUES
    ('00000000-0000-4000-8000-000000000003', 2.5), ('00000000-0000-4000-8000-000000000004', NULL);
INSERT INTO voc.staff_assignment_policy SELECT company_uuid, gen_random_uuid(), true FROM public.company;
INSERT INTO public.staff_assignment_policy SELECT company_uuid, staff_user_uuid, false FROM voc.staff_assignment_policy;
INSERT INTO voc.quick_reply
SELECT gen_random_uuid(), company_uuid, NULL, gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), 'saved reply'
FROM public.company;
INSERT INTO voc.messenger_destination
SELECT company_uuid, NULL, gen_random_uuid(), 'saved channel' FROM public.company;

CREATE TEMP TABLE existing_relations AS
SELECT * FROM (VALUES
    ('voc.bronze_message', 'voc.bronze_messages'::regclass),
    ('voc.silver_message', 'voc.silver_messages'::regclass),
    ('voc.staff_assignment_policy', 'voc.staff_assignment_policy'::regclass),
    ('public.staff_assignment_policy', 'public.staff_assignment_policy'::regclass)
) AS relations(name, original);

\ir rename_schema.sql
\ir rename_schema.sql

DO $$
BEGIN
    ASSERT NOT EXISTS (SELECT FROM existing_relations WHERE original IS DISTINCT FROM to_regclass(name));
    ASSERT (SELECT source_company_id='source-a' AND c.company_id='00000000-0000-4000-8000-000000000001'
            FROM voc.ticket_source_connection r JOIN public.company c USING (company_id));
    ASSERT (SELECT b.source_company_id='source-a' AND b.raw_payload_json='{"message":"original"}'
            FROM voc.bronze_message b JOIN voc.silver_message s USING (event_id));
    ASSERT (SELECT count(*)=2 AND count(avg_first_reply_7d_ms)=1 AND max(avg_first_reply_7d_ms)=2500
            FROM voc.feature_snapshot);
    ASSERT (SELECT v.is_enabled AND NOT p.is_enabled
            FROM voc.staff_assignment_policy v JOIN public.staff_assignment_policy p USING (company_id, staff_user_id));
    ASSERT (SELECT reply_id IS NOT NULL AND company_id IS NOT NULL AND owner_id IS NULL
                AND team_id IS NOT NULL AND domain_id IS NOT NULL AND category_id IS NOT NULL AND body='saved reply'
            FROM voc.quick_reply);
    ASSERT (SELECT company_id IS NOT NULL AND team_id IS NULL AND staff_user_id IS NOT NULL
                AND messenger_channel='saved channel' FROM voc.messenger_destination);
    BEGIN
        DELETE FROM public.company;
        RAISE EXCEPTION 'Company foreign key was lost';
    EXCEPTION WHEN foreign_key_violation THEN NULL;
    END;
END $$;
ROLLBACK;

BEGIN;
SET LOCAL plpgsql.check_asserts=on;
\ir schema.sql
\ir ticket_schema.sql
SAVEPOINT before_settings;

SET LOCAL search_path TO public;
\ir settings_schema.sql
SET LOCAL search_path TO voc, public;
\ir settings_schema.sql

DO $$ BEGIN
    ASSERT to_regclass('staff_assignment_policy') = 'public.staff_assignment_policy'::regclass;
    ASSERT to_regclass('quick_reply') = 'public.quick_reply'::regclass;
    ASSERT to_regclass('messenger_destination') = 'public.messenger_destination'::regclass;
END $$;
ROLLBACK TO before_settings;

SET LOCAL search_path TO voc, public;
\ir settings_schema.sql
SET LOCAL search_path TO public;
\ir settings_schema.sql

DO $$ BEGIN
    ASSERT to_regclass('public.staff_assignment_policy') IS NULL;
    ASSERT to_regclass('public.quick_reply') IS NULL;
    ASSERT to_regclass('public.messenger_destination') IS NULL;
END $$;
ROLLBACK;
