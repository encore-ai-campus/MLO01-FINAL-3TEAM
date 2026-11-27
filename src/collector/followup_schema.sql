CREATE UNIQUE INDEX IF NOT EXISTS ticket_company_uuid_key ON public.ticket(company_id, ticket_id);

CREATE TABLE IF NOT EXISTS public.chat (
    chat_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL REFERENCES public.company,
    customer_id uuid NOT NULL, domain_id uuid NOT NULL REFERENCES public.domain,
    channel_id uuid NOT NULL, status varchar(32) NOT NULL,
    waiting_at timestamptz NOT NULL, started_at timestamptz, ended_at timestamptz,
    assignee_id uuid, category_id uuid, sentiment varchar(32), embedding vector,
    UNIQUE (company_id, chat_id),
    FOREIGN KEY (company_id, customer_id) REFERENCES public.customer(company_id, customer_id),
    FOREIGN KEY (company_id, channel_id) REFERENCES public.channel(company_id, channel_id),
    FOREIGN KEY (company_id, assignee_id) REFERENCES public.staff_user(company_id, staff_user_id),
    FOREIGN KEY (domain_id, category_id) REFERENCES public.category(domain_id, category_id)
);

CREATE TABLE IF NOT EXISTS public.chat_message (
    chat_message_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id uuid NOT NULL REFERENCES public.chat,
    staff_user_id uuid REFERENCES public.staff_user,
    sender_type varchar(32) NOT NULL, message_type varchar(32) NOT NULL,
    body text NOT NULL, sent_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_message_history ON public.chat_message(chat_id, sent_at);

CREATE TABLE IF NOT EXISTS public.followup_track (
    company_id uuid NOT NULL, staff_user_id uuid NOT NULL, ticket_id uuid NOT NULL,
    tracked_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (company_id, staff_user_id, ticket_id),
    FOREIGN KEY (company_id, staff_user_id) REFERENCES public.staff_user(company_id, staff_user_id),
    FOREIGN KEY (company_id, ticket_id) REFERENCES public.ticket(company_id, ticket_id)
);

CREATE TABLE IF NOT EXISTS public.followup_contact (
    contact_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id uuid NOT NULL, ticket_id uuid, chat_id uuid,
    created_by_id uuid NOT NULL, assignee_id uuid NOT NULL,
    reason varchar(1000) NOT NULL, scheduled_at timestamptz NOT NULL,
    completed_by_id uuid, contacted_at timestamptz, result varchar(1000),
    CHECK (num_nonnulls(ticket_id, chat_id) = 1),
    CHECK (num_nonnulls(completed_by_id, contacted_at, result) IN (0, 3)),
    FOREIGN KEY (company_id, ticket_id) REFERENCES public.ticket(company_id, ticket_id),
    FOREIGN KEY (company_id, chat_id) REFERENCES public.chat(company_id, chat_id),
    FOREIGN KEY (company_id, created_by_id) REFERENCES public.staff_user(company_id, staff_user_id),
    FOREIGN KEY (company_id, assignee_id) REFERENCES public.staff_user(company_id, staff_user_id),
    FOREIGN KEY (company_id, completed_by_id) REFERENCES public.staff_user(company_id, staff_user_id)
);
CREATE INDEX IF NOT EXISTS followup_contact_owner ON public.followup_contact(company_id, created_by_id);
CREATE INDEX IF NOT EXISTS followup_contact_assignee ON public.followup_contact(company_id, assignee_id);

ALTER TABLE public.followup_contact ADD COLUMN IF NOT EXISTS reminders_started_at timestamptz;
ALTER TABLE public.followup_contact ADD COLUMN IF NOT EXISTS reminders_stopped_at timestamptz;

CREATE INDEX IF NOT EXISTS followup_silver_conversation ON voc.silver_message
    (source_company_id, source_channel_type, source_connection_id, source_conversation_id, standardized_at)
    WHERE schema_version=4 AND message_kind='customer_inquiry';
