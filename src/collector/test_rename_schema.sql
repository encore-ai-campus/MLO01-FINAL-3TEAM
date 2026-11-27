-- Run in an empty disposable database. The fixture and its migration are rolled back.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL plpgsql.check_asserts=on;
CREATE SCHEMA voc;
CREATE TABLE public.company (company_uuid uuid PRIMARY KEY);
CREATE TABLE voc.ticket_source_connection (
    company_id text PRIMARY KEY, company_uuid uuid REFERENCES public.company
);
CREATE TABLE voc.bronze_messages (event_id uuid PRIMARY KEY, raw_payload jsonb);
CREATE TABLE voc.silver_messages (event_id uuid PRIMARY KEY REFERENCES voc.bronze_messages);
CREATE TABLE voc.feature_snapshot (feature_uuid uuid PRIMARY KEY, avg_first_reply_7d_seconds double precision);
INSERT INTO public.company VALUES ('00000000-0000-4000-8000-000000000001');
INSERT INTO voc.ticket_source_connection VALUES ('source-a', '00000000-0000-4000-8000-000000000001');
INSERT INTO voc.bronze_messages VALUES ('00000000-0000-4000-8000-000000000002', '{"message":"original"}');
INSERT INTO voc.silver_messages SELECT event_id FROM voc.bronze_messages;
INSERT INTO voc.feature_snapshot VALUES
    ('00000000-0000-4000-8000-000000000003', 2.5), ('00000000-0000-4000-8000-000000000004', NULL);

\ir rename_schema.sql
\ir rename_schema.sql

DO $$
BEGIN
    ASSERT (SELECT source_company_id='source-a' AND c.company_id='00000000-0000-4000-8000-000000000001'
            FROM voc.ticket_source_connection r JOIN public.company c USING (company_id));
    ASSERT (SELECT b.raw_payload_json='{"message":"original"}'
            FROM voc.bronze_message b JOIN voc.silver_message s USING (event_id));
    ASSERT (SELECT count(*)=2 AND count(avg_first_reply_7d_ms)=1 AND max(avg_first_reply_7d_ms)=2500
            FROM voc.feature_snapshot);
    BEGIN
        DELETE FROM public.company;
        RAISE EXCEPTION 'Company foreign key was lost';
    EXCEPTION WHEN foreign_key_violation THEN NULL;
    END;
END $$;
ROLLBACK;
