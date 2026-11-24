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
CREATE TABLE IF NOT EXISTS voc.standardize_outbox (
    event_id uuid PRIMARY KEY REFERENCES voc.bronze_messages,
    queued_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz
);
CREATE INDEX IF NOT EXISTS standardize_pending ON voc.standardize_outbox (queued_at) WHERE published_at IS NULL;
CREATE TABLE IF NOT EXISTS voc.silver_messages (
    event_id uuid PRIMARY KEY REFERENCES voc.bronze_messages,
    schema_version integer NOT NULL,
    company_id text NOT NULL, channel text NOT NULL, connection_id text NOT NULL,
    source_message_id text NOT NULL, source_conversation_id text NOT NULL, reply_to_message_id text,
    sender_ref text, title text NOT NULL, body_text text NOT NULL,
    occurred_at timestamptz, received_at timestamptz NOT NULL,
    classification_status text NOT NULL DEFAULT 'pending',
    standardized_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS voc.review_events (
    topic text NOT NULL, partition_id integer NOT NULL, kafka_offset bigint NOT NULL,
    event_id text, reason text NOT NULL, recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (topic, partition_id, kafka_offset)
);
CREATE UNIQUE INDEX IF NOT EXISTS review_event_reason ON voc.review_events (event_id, reason) WHERE event_id IS NOT NULL;
