CREATE UNIQUE INDEX IF NOT EXISTS ticket_company_uuid_key ON public.ticket(company_uuid, ticket_uuid);

CREATE TABLE IF NOT EXISTS public.chat (
    chat_uuid uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_uuid uuid NOT NULL REFERENCES public.company,
    customer_uuid uuid NOT NULL, domain_uuid uuid NOT NULL REFERENCES public.domain,
    channel_uuid uuid NOT NULL, status varchar(32) NOT NULL,
    waiting_at timestamptz NOT NULL, started_at timestamptz, ended_at timestamptz,
    assignee_uuid uuid, category_uuid uuid, sentiment varchar(32), embedding vector,
    UNIQUE (company_uuid, chat_uuid),
    FOREIGN KEY (company_uuid, customer_uuid) REFERENCES public.customer(company_uuid, customer_uuid),
    FOREIGN KEY (company_uuid, channel_uuid) REFERENCES public.channel(company_uuid, channel_uuid),
    FOREIGN KEY (company_uuid, assignee_uuid) REFERENCES public.staff_user(company_uuid, staff_user_uuid),
    FOREIGN KEY (domain_uuid, category_uuid) REFERENCES public.category(domain_uuid, category_uuid)
);

CREATE TABLE IF NOT EXISTS public.chat_message (
    chat_message_uuid uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_uuid uuid NOT NULL REFERENCES public.chat,
    staff_user_uuid uuid REFERENCES public.staff_user,
    sender_type varchar(32) NOT NULL, message_type varchar(32) NOT NULL,
    body text NOT NULL, sent_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_message_history ON public.chat_message(chat_uuid, sent_at);

CREATE TABLE IF NOT EXISTS public.followup_track (
    company_uuid uuid NOT NULL, staff_user_uuid uuid NOT NULL, ticket_uuid uuid NOT NULL,
    tracked_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (company_uuid, staff_user_uuid, ticket_uuid),
    FOREIGN KEY (company_uuid, staff_user_uuid) REFERENCES public.staff_user(company_uuid, staff_user_uuid),
    FOREIGN KEY (company_uuid, ticket_uuid) REFERENCES public.ticket(company_uuid, ticket_uuid)
);

CREATE TABLE IF NOT EXISTS public.followup_contact (
    contact_uuid uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_uuid uuid NOT NULL, ticket_uuid uuid, chat_uuid uuid,
    created_by_uuid uuid NOT NULL, assignee_uuid uuid NOT NULL,
    reason varchar(1000) NOT NULL, scheduled_at timestamptz NOT NULL,
    completed_by_uuid uuid, contacted_at timestamptz, result varchar(1000),
    CHECK (num_nonnulls(ticket_uuid, chat_uuid) = 1),
    CHECK (num_nonnulls(completed_by_uuid, contacted_at, result) IN (0, 3)),
    FOREIGN KEY (company_uuid, ticket_uuid) REFERENCES public.ticket(company_uuid, ticket_uuid),
    FOREIGN KEY (company_uuid, chat_uuid) REFERENCES public.chat(company_uuid, chat_uuid),
    FOREIGN KEY (company_uuid, created_by_uuid) REFERENCES public.staff_user(company_uuid, staff_user_uuid),
    FOREIGN KEY (company_uuid, assignee_uuid) REFERENCES public.staff_user(company_uuid, staff_user_uuid),
    FOREIGN KEY (company_uuid, completed_by_uuid) REFERENCES public.staff_user(company_uuid, staff_user_uuid)
);
CREATE INDEX IF NOT EXISTS followup_contact_owner ON public.followup_contact(company_uuid, created_by_uuid);
CREATE INDEX IF NOT EXISTS followup_contact_assignee ON public.followup_contact(company_uuid, assignee_uuid);

ALTER TABLE public.followup_contact ADD COLUMN IF NOT EXISTS reminders_started_at timestamptz;
ALTER TABLE public.followup_contact ADD COLUMN IF NOT EXISTS reminders_stopped_at timestamptz;

CREATE INDEX IF NOT EXISTS followup_silver_conversation ON voc.silver_messages
    (company_id, channel, connection_id, source_conversation_id, standardized_at)
    WHERE schema_version=4 AND message_kind='customer_inquiry';
