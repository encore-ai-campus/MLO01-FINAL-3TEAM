-- Apply after schema.sql and ticket_schema.sql. A NULL start time disables automatic intake.
ALTER TABLE voc.ticket_source_connection ADD COLUMN IF NOT EXISTS auto_customer_from timestamptz;
ALTER TABLE voc.standardize_outbox ADD COLUMN IF NOT EXISTS auto_customer_confirmed_at timestamptz;

-- Updating the intake policy does not remap already confirmed tickets.
CREATE OR REPLACE TRIGGER ticket_source_registered
    AFTER INSERT OR UPDATE OF company_id,channel,connection_id,source_channel_id,company_uuid,channel_uuid
    ON voc.ticket_source_connection FOR EACH ROW EXECUTE FUNCTION voc.project_source_trigger();

-- Use Flink's first normalized role. Only an undecided, eligible inbound message
-- on an enabled route can request the existing role-confirmation/replay flow.
CREATE OR REPLACE FUNCTION voc.confirm_channel_intake(source_event uuid) RETURNS void
LANGUAGE sql VOLATILE AS $$
    UPDATE voc.standardize_outbox o
    SET verified_message_kind='customer_inquiry', auto_customer_confirmed_at=clock_timestamp()
    FROM voc.silver_messages s
    JOIN voc.bronze_messages b ON b.event_id=s.event_id
    JOIN voc.ticket_source_connection r ON (r.company_id,r.channel,r.connection_id,r.source_channel_id)
        =(s.company_id,s.channel,s.connection_id,coalesce(s.source_channel_id,''))
    WHERE s.event_id=source_event AND s.schema_version=4 AND s.message_kind='unknown'
      AND nullif(btrim(s.sender_ref),'') IS NOT NULL
      AND b.received_at >= r.auto_customer_from
      AND (o.event_id,o.target_schema_version,o.body_version)=(s.event_id,s.schema_version,s.body_version)
      AND o.verified_message_kind IS NULL AND o.auto_customer_confirmed_at IS NULL
      AND ((s.channel='discord' AND b.origin IN ('discord_gateway','discord_history')
            AND b.context->>'channel_id'=s.source_channel_id)
        OR (s.channel='gmail' AND b.origin='gmail_api' AND b.context->>'label'='VOC'
            AND NOT ((s.source_metadata->'label_ids') ?| ARRAY['SENT','DRAFT','SPAM','TRASH'])));
$$;

CREATE OR REPLACE FUNCTION voc.confirm_new_channel_intake() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- Serialize enabling a tenant route with the first insertion of its Silver row.
    PERFORM pg_advisory_xact_lock_shared(hashtextextended('channel-intake:' || NEW.company_id, 0));
    PERFORM voc.confirm_channel_intake(NEW.event_id);
    RETURN NEW;
END $$;

CREATE OR REPLACE TRIGGER confirm_new_channel_intake
    AFTER INSERT ON voc.silver_messages FOR EACH ROW
    WHEN (NEW.schema_version=4 AND NEW.message_kind='unknown')
    EXECUTE FUNCTION voc.confirm_new_channel_intake();

CREATE OR REPLACE FUNCTION voc.confirm_enabled_channel_intake() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='UPDATE' AND OLD.auto_customer_from IS NOT DISTINCT FROM NEW.auto_customer_from THEN
        RETURN NEW;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('channel-intake:' || NEW.company_id, 0));
    PERFORM voc.confirm_channel_intake(s.event_id)
    FROM voc.silver_messages s
    WHERE (s.company_id,s.channel,s.connection_id,coalesce(s.source_channel_id,''))
         =(NEW.company_id,NEW.channel,NEW.connection_id,NEW.source_channel_id)
      AND s.schema_version=4 AND s.message_kind='unknown' AND s.received_at >= NEW.auto_customer_from;
    RETURN NEW;
END $$;

CREATE OR REPLACE TRIGGER confirm_enabled_channel_intake
    AFTER INSERT OR UPDATE OF auto_customer_from ON voc.ticket_source_connection FOR EACH ROW
    WHEN (NEW.auto_customer_from IS NOT NULL)
    EXECUTE FUNCTION voc.confirm_enabled_channel_intake();
