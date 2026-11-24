CREATE SCHEMA IF NOT EXISTS voc;
CREATE TABLE IF NOT EXISTS voc.bronze_messages (
    event_id uuid PRIMARY KEY,
    company_id text NOT NULL,
    channel text NOT NULL CHECK (channel IN ('discord','gmail')),
    connection_id text NOT NULL,
    source_message_id text NOT NULL,
    origin text NOT NULL,
    received_at timestamptz NOT NULL,
    context jsonb NOT NULL,
    raw_payload jsonb NOT NULL,
    UNIQUE (company_id, channel, connection_id, source_message_id)
);

-- Retain the complete v1 state, including rows that previously went to review.
DO $$ BEGIN
    IF EXISTS (SELECT FROM information_schema.columns WHERE table_schema='voc'
               AND table_name='silver_messages' AND column_name='classification_status') THEN
        ALTER TABLE voc.silver_messages RENAME TO silver_messages_v1;
    END IF;
    IF EXISTS (SELECT FROM information_schema.tables WHERE table_schema='voc' AND table_name='standardize_outbox')
       AND NOT EXISTS (SELECT FROM information_schema.columns WHERE table_schema='voc'
                       AND table_name='standardize_outbox' AND column_name='target_schema_version') THEN
        ALTER TABLE voc.standardize_outbox RENAME TO standardize_outbox_v1;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS voc.standardize_outbox (
    event_id uuid NOT NULL REFERENCES voc.bronze_messages,
    target_schema_version integer NOT NULL DEFAULT 4 CHECK (target_schema_version = 4),
    body_version integer NOT NULL DEFAULT 1 CHECK (body_version > 0),
    verified_message_kind text CHECK (verified_message_kind IN ('customer_inquiry','staff_reply','other','unknown')),
    queued_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    PRIMARY KEY (event_id, target_schema_version)
);
CREATE INDEX IF NOT EXISTS standardize_pending_v4 ON voc.standardize_outbox (queued_at, event_id)
    WHERE published_at IS NULL;

-- An operator may confirm the sender or approve a new normalization of the immutable body.
CREATE OR REPLACE FUNCTION voc.requeue_standardization() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.body_version < OLD.body_version THEN
        RAISE EXCEPTION 'body_version cannot decrease';
    END IF;
    IF NEW.body_version > OLD.body_version AND NOT EXISTS (
        SELECT FROM voc.silver_messages WHERE event_id=OLD.event_id AND schema_version=OLD.target_schema_version
            AND body_version=OLD.body_version
    ) THEN
        RAISE EXCEPTION 'store the current Silver body before approving its next version';
    END IF;
    IF NEW.body_version IS DISTINCT FROM OLD.body_version
       OR NEW.verified_message_kind IS DISTINCT FROM OLD.verified_message_kind THEN
        NEW.queued_at := now();
        NEW.published_at := NULL;
    END IF;
    RETURN NEW;
END $$;
CREATE OR REPLACE TRIGGER standardize_request_changed BEFORE UPDATE OF body_version, verified_message_kind
    ON voc.standardize_outbox FOR EACH ROW EXECUTE FUNCTION voc.requeue_standardization();

CREATE TABLE IF NOT EXISTS voc.silver_messages (
    schema_version integer NOT NULL CHECK (schema_version = 4),
    event_id uuid NOT NULL REFERENCES voc.bronze_messages,
    company_id text NOT NULL,
    channel text NOT NULL CHECK (channel IN ('discord','gmail')),
    connection_id text NOT NULL,
    source_message_id text NOT NULL,
    source_conversation_id text,
    source_channel_id text,
    source_channel_name text,
    reply_to_message_id text,
    message_kind text NOT NULL CHECK (message_kind IN ('customer_inquiry','staff_reply','other','unknown')),
    sender_ref text,
    sender_display_name text,
    title text,
    body_text text NOT NULL CHECK (btrim(body_text) <> ''),
    body_version integer NOT NULL CHECK (body_version > 0),
    occurred_at timestamptz(6),
    received_at timestamptz(6) NOT NULL,
    source_metadata jsonb NOT NULL,
    standardized_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, schema_version),
    CHECK (CASE WHEN channel = 'discord' THEN
               source_channel_id IS NOT NULL AND title IS NULL AND source_metadata = '{}'::jsonb
               AND (source_conversation_id IS NOT NULL OR reply_to_message_id IS NOT NULL)
           ELSE source_channel_id IS NULL AND source_channel_name IS NULL AND reply_to_message_id IS NULL
               AND source_conversation_id IS NOT NULL AND jsonb_typeof(source_metadata) = 'object'
               AND source_metadata ?& ARRAY['label_ids','to_addresses']
               AND (source_metadata - ARRAY['label_ids','to_addresses']) = '{}'::jsonb
               AND jsonb_typeof(source_metadata->'label_ids') = 'array'
               AND jsonb_typeof(source_metadata->'to_addresses') = 'array'
               AND NOT jsonb_path_exists(source_metadata, '$.label_ids[*] ? (@.type() != "string")')
               AND NOT jsonb_path_exists(source_metadata, '$.to_addresses[*] ? (@.type() != "string")')
           END)
);
CREATE INDEX IF NOT EXISTS discord_silver_parent ON voc.silver_messages
    (company_id, connection_id, source_channel_id, source_message_id) WHERE channel='discord';
CREATE INDEX IF NOT EXISTS discord_silver_orphans ON voc.silver_messages
    (company_id, connection_id, source_channel_id, reply_to_message_id)
    WHERE channel='discord' AND source_conversation_id IS NULL;

-- Discord reply references may explicitly point outside the receiving guild or room.
CREATE OR REPLACE FUNCTION voc.discord_parent(message jsonb, guild text, room text) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT CASE WHEN COALESCE(message->>'type','19') = '19'
                     AND COALESCE(message#>>'{message_reference,type}','0') = '0'
                     AND message->>'channel_id' = room
                     AND COALESCE(message->>'guild_id', guild) = guild
                     AND COALESCE(message#>>'{message_reference,guild_id}', guild) = guild
                     AND COALESCE(message#>>'{message_reference,channel_id}', room) = room
                THEN message#>>'{message_reference,message_id}' END
$$;

CREATE TABLE IF NOT EXISTS voc.review_events (
    topic text NOT NULL, partition_id integer NOT NULL, kafka_offset bigint NOT NULL,
    event_id text, reason text NOT NULL, recorded_at timestamptz NOT NULL DEFAULT now(),
    schema_version integer NOT NULL,
    PRIMARY KEY (topic, partition_id, kafka_offset)
);
-- Rows in the old table were written by the v1 consumer.
ALTER TABLE voc.review_events ADD COLUMN IF NOT EXISTS schema_version integer NOT NULL DEFAULT 1;
CREATE UNIQUE INDEX IF NOT EXISTS review_event_version_reason ON voc.review_events
    (event_id, schema_version, reason) WHERE event_id IS NOT NULL;
DROP INDEX IF EXISTS voc.review_event_reason;

-- Queue every Bronze row, including old review-only events. Restarts preserve publish state and approvals.
INSERT INTO voc.standardize_outbox (event_id, target_schema_version)
    SELECT event_id, 4 FROM voc.bronze_messages ON CONFLICT (event_id, target_schema_version) DO NOTHING;
