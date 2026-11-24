-- The login service and ticket service use the same company and employee IDs.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS public.domain (
    domain_uuid uuid PRIMARY KEY DEFAULT gen_random_uuid(), name text NOT NULL
);
CREATE TABLE IF NOT EXISTS public.company (
    company_uuid uuid PRIMARY KEY DEFAULT gen_random_uuid(), email_domain text NOT NULL
);
-- Existing login-only installations can configure their domain before registering a source.
ALTER TABLE public.company ADD COLUMN IF NOT EXISTS name text;
ALTER TABLE public.company ADD COLUMN IF NOT EXISTS domain_uuid uuid REFERENCES public.domain;
CREATE TABLE IF NOT EXISTS public.team (
    team_uuid uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_uuid uuid NOT NULL REFERENCES public.company, name text NOT NULL,
    UNIQUE (company_uuid, team_uuid)
);
CREATE TABLE IF NOT EXISTS public.staff_user (
    staff_user_uuid uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_uuid uuid NOT NULL REFERENCES public.company,
    email text NOT NULL, password_hash text NOT NULL
);
ALTER TABLE public.staff_user ADD COLUMN IF NOT EXISTS name text;
ALTER TABLE public.staff_user ADD COLUMN IF NOT EXISTS team_uuid uuid REFERENCES public.team;
CREATE UNIQUE INDEX IF NOT EXISTS ticket_staff_company_key ON public.staff_user(company_uuid, staff_user_uuid);
CREATE TABLE IF NOT EXISTS public.permission (
    permission_uuid uuid PRIMARY KEY DEFAULT gen_random_uuid(), code text NOT NULL UNIQUE, name text
);
INSERT INTO public.permission(code, name) VALUES ('ticket.read', '티켓 조회') ON CONFLICT (code) DO NOTHING;
CREATE TABLE IF NOT EXISTS public.staff_permission (
    staff_user_uuid uuid NOT NULL REFERENCES public.staff_user ON DELETE CASCADE,
    permission_uuid uuid NOT NULL REFERENCES public.permission ON DELETE CASCADE,
    PRIMARY KEY(staff_user_uuid, permission_uuid)
);
CREATE TABLE IF NOT EXISTS public.staff_session (
    token_hash text PRIMARY KEY, staff_user_uuid uuid NOT NULL REFERENCES public.staff_user ON DELETE CASCADE,
    expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS staff_session_expiry ON public.staff_session(expires_at);
CREATE TABLE IF NOT EXISTS public.channel (
    channel_uuid uuid PRIMARY KEY DEFAULT gen_random_uuid(), company_uuid uuid NOT NULL REFERENCES public.company,
    name text NOT NULL, type text NOT NULL, UNIQUE(company_uuid, channel_uuid, type),
    UNIQUE(company_uuid, channel_uuid)
);
CREATE TABLE IF NOT EXISTS public.category (
    category_uuid uuid PRIMARY KEY DEFAULT gen_random_uuid(), domain_uuid uuid NOT NULL REFERENCES public.domain,
    name text NOT NULL, UNIQUE (domain_uuid, category_uuid)
);
CREATE TABLE IF NOT EXISTS public.customer (
    customer_uuid uuid PRIMARY KEY DEFAULT gen_random_uuid(), company_uuid uuid NOT NULL REFERENCES public.company,
    source_type text NOT NULL, external_customer_id text NOT NULL, name text, email text,
    UNIQUE(company_uuid, source_type, external_customer_id), UNIQUE(company_uuid, customer_uuid)
);
CREATE TABLE IF NOT EXISTS voc.ticket_source_connection (
    company_id text NOT NULL, channel text NOT NULL, connection_id text NOT NULL,
    source_channel_id text NOT NULL DEFAULT '',
    company_uuid uuid NOT NULL, channel_uuid uuid NOT NULL,
    PRIMARY KEY(company_id, channel, connection_id, source_channel_id),
    FOREIGN KEY(company_uuid, channel_uuid, channel) REFERENCES public.channel(company_uuid, channel_uuid, type),
    CHECK ((channel = 'gmail' AND source_channel_id = '') OR
           (channel = 'discord' AND source_channel_id <> ''))
);
CREATE TABLE IF NOT EXISTS public.ticket (
    ticket_uuid uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_event_id uuid NOT NULL UNIQUE REFERENCES voc.bronze_messages(event_id),
    company_uuid uuid NOT NULL REFERENCES public.company, domain_uuid uuid NOT NULL REFERENCES public.domain,
    channel_uuid uuid NOT NULL, customer_uuid uuid NOT NULL,
    ticket_number text NOT NULL CHECK(ticket_number ~ '^[0-9]{8}$'),
    title varchar(50) NOT NULL, status text NOT NULL DEFAULT '미처리' CHECK(status IN ('미처리','보류','답변완료')),
    assigned_team_uuid uuid, assignee_uuid uuid, category_uuid uuid,
    sentiment text CHECK(sentiment IN ('긍정','중립','부정','매우 부정')), embedding vector,
    sentiment_status text NOT NULL DEFAULT 'pending' CHECK(sentiment_status IN ('pending','running','succeeded','failed')),
    category_status text NOT NULL DEFAULT 'pending' CHECK(category_status IN ('pending','running','succeeded','failed')),
    created_at timestamptz NOT NULL, updated_at timestamptz NOT NULL,
    UNIQUE(company_uuid, ticket_number),
    FOREIGN KEY(company_uuid, channel_uuid) REFERENCES public.channel(company_uuid, channel_uuid),
    FOREIGN KEY(company_uuid, customer_uuid) REFERENCES public.customer(company_uuid, customer_uuid),
    FOREIGN KEY(company_uuid, assigned_team_uuid) REFERENCES public.team(company_uuid, team_uuid),
    FOREIGN KEY(company_uuid, assignee_uuid) REFERENCES public.staff_user(company_uuid, staff_user_uuid),
    FOREIGN KEY(domain_uuid, category_uuid) REFERENCES public.category(domain_uuid, category_uuid)
);
CREATE INDEX IF NOT EXISTS ticket_company_order ON public.ticket(company_uuid,created_at,ticket_number);
CREATE INDEX IF NOT EXISTS ticket_assignee_order ON public.ticket(company_uuid,assignee_uuid,created_at,ticket_number);
CREATE INDEX IF NOT EXISTS ticket_customer_history ON public.ticket(company_uuid,customer_uuid,created_at);
-- Only explicit cross-channel candidates appear. The pair is symmetric and never merges tickets.
CREATE TABLE IF NOT EXISTS public.ticket_duplicate_candidate (
    ticket_uuid uuid NOT NULL REFERENCES public.ticket ON DELETE CASCADE,
    candidate_ticket_uuid uuid NOT NULL REFERENCES public.ticket ON DELETE CASCADE,
    PRIMARY KEY(ticket_uuid,candidate_ticket_uuid), CHECK(ticket_uuid<candidate_ticket_uuid)
);
CREATE INDEX IF NOT EXISTS ticket_candidate_reverse ON public.ticket_duplicate_candidate(candidate_ticket_uuid);
CREATE OR REPLACE FUNCTION voc.check_ticket_candidate() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.ticket_uuid > NEW.candidate_ticket_uuid THEN
        SELECT NEW.candidate_ticket_uuid,NEW.ticket_uuid INTO NEW.ticket_uuid,NEW.candidate_ticket_uuid;
    END IF;
    IF NOT EXISTS (SELECT FROM public.ticket a JOIN public.ticket b ON a.company_uuid=b.company_uuid
                   JOIN public.channel ac ON ac.channel_uuid=a.channel_uuid
                   JOIN public.channel bc ON bc.channel_uuid=b.channel_uuid
                   WHERE a.ticket_uuid=NEW.ticket_uuid AND b.ticket_uuid=NEW.candidate_ticket_uuid
                     AND ac.type<>bc.type) THEN
        RAISE EXCEPTION 'ticket candidates must belong to the same company and different channels'
              USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER validate_ticket_candidate BEFORE INSERT OR UPDATE ON public.ticket_duplicate_candidate
    FOR EACH ROW EXECUTE FUNCTION voc.check_ticket_candidate();
CREATE TABLE IF NOT EXISTS public.ticket_message (
    ticket_message_uuid uuid PRIMARY KEY DEFAULT gen_random_uuid(), ticket_uuid uuid NOT NULL REFERENCES public.ticket,
    source_event_id uuid UNIQUE REFERENCES voc.bronze_messages, staff_user_uuid uuid REFERENCES public.staff_user,
    body text NOT NULL, body_version integer, sender_type text NOT NULL CHECK(sender_type IN ('customer','staff')),
    message_type text NOT NULL CHECK(message_type IN ('inquiry','reply','memo')), delivery_status text,
    created_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS ticket_message_history ON public.ticket_message(ticket_uuid,created_at);
-- A role correction never deletes staff work. Reconfirmation reveals the original ticket.
CREATE OR REPLACE VIEW public.visible_ticket AS
SELECT t.* FROM public.ticket t JOIN voc.silver_messages s
    ON s.event_id=t.source_event_id AND s.schema_version=4 WHERE s.message_kind='customer_inquiry';

CREATE TABLE IF NOT EXISTS voc.ticket_number_counter (
    company_uuid uuid PRIMARY KEY REFERENCES public.company,
    last_number integer NOT NULL CHECK(last_number BETWEEN 1 AND 99999999)
);
CREATE TABLE IF NOT EXISTS voc.ticket_projection_issue (
    event_id uuid PRIMARY KEY REFERENCES voc.bronze_messages, reason text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);
-- Materialize the exact model input and its two pending jobs in the same transaction as the ticket.
CREATE TABLE IF NOT EXISTS voc.model_text_history (
    company_uuid uuid NOT NULL REFERENCES public.company, event_id uuid NOT NULL REFERENCES voc.bronze_messages,
    preprocess_version integer NOT NULL, body_version integer NOT NULL, silver_schema_version integer NOT NULL,
    model_text text NOT NULL, available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(company_uuid,event_id,preprocess_version,body_version)
);
CREATE TABLE IF NOT EXISTS voc.ticket_analysis_outbox (
    company_uuid uuid NOT NULL, event_id uuid NOT NULL, preprocess_version integer NOT NULL, body_version integer NOT NULL,
    analysis_kind text NOT NULL CHECK(analysis_kind IN ('sentiment','category')),
    queued_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz,
    PRIMARY KEY(company_uuid,event_id,preprocess_version,body_version,analysis_kind),
    FOREIGN KEY(company_uuid,event_id,preprocess_version,body_version) REFERENCES voc.model_text_history
);

CREATE OR REPLACE FUNCTION voc.ticket_display_title(subject text, body text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT coalesce(nullif(rtrim(left(
        btrim(regexp_replace(coalesce(nullif(btrim(subject),''),
            split_part(regexp_replace(body, '^[[:space:]]+', ''), E'\n', 1)),
            '[^가-힣ㄱ-ㅎㅏ-ㅣa-zA-Z0-9]+', ' ', 'g')),
        CASE WHEN nullif(btrim(subject),'') IS NULL THEN 40 ELSE 50 END)), ''), '문의')
$$;

CREATE OR REPLACE FUNCTION voc.project_ticket(source_id uuid) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    s voc.silver_messages%ROWTYPE;
    destination record;
    ticket_id uuid;
    owner_id uuid;
    customer_id uuid;
    sequence_number integer;
    external_id text;
    issue text;
BEGIN
    SELECT * INTO STRICT s FROM voc.silver_messages WHERE event_id=source_id AND schema_version=4;
    -- A route can be registered concurrently with the arrival of its first message.
    -- A fresh statement after the lock sees the winning transaction in READ COMMITTED.
    PERFORM pg_advisory_xact_lock(hashtextextended('ticket-route:' || s.company_id, 0));
    -- Re-read after the lock so backfills never publish a superseded message kind or version.
    SELECT * INTO STRICT s FROM voc.silver_messages WHERE event_id=source_id AND schema_version=4;
    IF s.message_kind <> 'customer_inquiry' THEN
        DELETE FROM voc.ticket_projection_issue WHERE event_id=source_id;
        DELETE FROM voc.ticket_analysis_outbox WHERE event_id=source_id AND published_at IS NULL;
        RETURN;
    END IF;
    SELECT ticket_uuid,company_uuid INTO ticket_id,owner_id FROM public.ticket WHERE source_event_id=source_id;
    IF ticket_id IS NULL THEN
        SELECT c.company_uuid, c.domain_uuid, r.channel_uuid INTO destination
        FROM voc.ticket_source_connection r JOIN public.company c USING(company_uuid)
        WHERE (r.company_id,r.channel,r.connection_id,r.source_channel_id) =
              (s.company_id,s.channel,s.connection_id,coalesce(s.source_channel_id,''));
        issue := CASE WHEN NOT FOUND THEN 'unregistered_source'
                      WHEN destination.domain_uuid IS NULL THEN 'missing_company_domain'
                      WHEN nullif(btrim(s.sender_ref),'') IS NULL THEN 'missing_sender' END;
        IF issue IS NOT NULL THEN
            INSERT INTO voc.ticket_projection_issue(event_id,reason) VALUES (source_id,issue)
            ON CONFLICT (event_id) DO UPDATE SET reason=excluded.reason
            WHERE voc.ticket_projection_issue.reason IS DISTINCT FROM excluded.reason;
            RETURN;
        END IF;
        owner_id := destination.company_uuid;
        INSERT INTO voc.ticket_number_counter(company_uuid,last_number) VALUES (owner_id,1)
        ON CONFLICT (company_uuid) DO UPDATE SET last_number=voc.ticket_number_counter.last_number+1
        RETURNING last_number INTO sequence_number;
        external_id := CASE WHEN s.channel='gmail' THEN lower(s.sender_ref) ELSE s.sender_ref END;
        INSERT INTO public.customer(company_uuid,source_type,external_customer_id,name,email)
        VALUES (owner_id,s.channel,external_id,s.sender_display_name,CASE WHEN s.channel='gmail' THEN s.sender_ref END)
        ON CONFLICT(company_uuid,source_type,external_customer_id) DO UPDATE SET name=coalesce(customer.name,excluded.name)
        RETURNING customer_uuid INTO customer_id;
        INSERT INTO public.ticket(source_event_id,company_uuid,domain_uuid,channel_uuid,customer_uuid,
                                  ticket_number,title,created_at,updated_at)
        VALUES (source_id,owner_id,destination.domain_uuid,destination.channel_uuid,customer_id,
                lpad(sequence_number::text,8,'0'),voc.ticket_display_title(s.title,s.body_text),s.received_at,s.received_at)
        RETURNING ticket_uuid INTO ticket_id;
    END IF;
    DELETE FROM voc.ticket_projection_issue WHERE event_id=source_id;
    INSERT INTO public.ticket_message(ticket_uuid,source_event_id,body,body_version,sender_type,message_type,created_at)
    VALUES (ticket_id,source_id,s.body_text,s.body_version,'customer','inquiry',coalesce(s.occurred_at,s.received_at))
    ON CONFLICT(source_event_id) DO UPDATE SET body=excluded.body,body_version=excluded.body_version
    WHERE ticket_message.body_version < excluded.body_version;
    INSERT INTO voc.model_text_history(company_uuid,event_id,preprocess_version,body_version,silver_schema_version,model_text)
    VALUES(owner_id,source_id,1,s.body_version,s.schema_version,s.body_text) ON CONFLICT DO NOTHING;
    INSERT INTO voc.ticket_analysis_outbox(company_uuid,event_id,preprocess_version,body_version,analysis_kind)
    VALUES(owner_id,source_id,1,s.body_version,'sentiment'),(owner_id,source_id,1,s.body_version,'category')
    ON CONFLICT DO NOTHING;
END $$;

CREATE OR REPLACE FUNCTION voc.project_silver_trigger() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.message_kind='customer_inquiry' OR (TG_OP='UPDATE' AND OLD.message_kind='customer_inquiry') THEN
        PERFORM voc.project_ticket(NEW.event_id);
    END IF;
    RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER project_customer_ticket
    AFTER INSERT OR UPDATE OF message_kind,body_version,sender_ref ON voc.silver_messages
    FOR EACH ROW EXECUTE FUNCTION voc.project_silver_trigger();

CREATE OR REPLACE FUNCTION voc.project_source_trigger() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE source_id uuid;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('ticket-route:' || NEW.company_id, 0));
    FOR source_id IN SELECT s.event_id FROM voc.silver_messages s WHERE s.schema_version=4
        AND s.message_kind='customer_inquiry' AND
        (s.company_id,s.channel,s.connection_id,coalesce(s.source_channel_id,'')) =
        (NEW.company_id,NEW.channel,NEW.connection_id,NEW.source_channel_id)
        ORDER BY s.received_at,s.event_id
    LOOP
        PERFORM voc.project_ticket(source_id);
    END LOOP;
    RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER ticket_source_registered
    AFTER INSERT OR UPDATE ON voc.ticket_source_connection FOR EACH ROW EXECUTE FUNCTION voc.project_source_trigger();

-- Cover confirmed v4 records that predate this migration or were held for configuration.
SELECT voc.project_ticket(s.event_id)
FROM voc.silver_messages s
WHERE s.message_kind='customer_inquiry' AND s.schema_version=4
AND NOT EXISTS (SELECT FROM public.ticket t WHERE t.source_event_id=s.event_id)
ORDER BY s.received_at,s.event_id;
