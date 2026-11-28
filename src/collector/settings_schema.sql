ALTER TABLE staff_user ADD COLUMN IF NOT EXISTS role text;
ALTER TABLE staff_user ADD COLUMN IF NOT EXISTS display_preferences_json jsonb NOT NULL DEFAULT '{}';

INSERT INTO permission(code, name) VALUES
    ('ticket.handle', '티켓 상담'),
    ('chat.handle', '채팅 상담'),
    ('quick_reply.team.edit', '팀별 신속답변 편집')
ON CONFLICT (code) DO NOTHING;

-- Preserve existing dashboard access once. Re-running schema setup must respect revocations.
WITH dashboard AS (
    INSERT INTO permission(code, name) VALUES ('analytics.read', 'VoC 대시보드 접근')
    ON CONFLICT (code) DO NOTHING
    RETURNING permission_id
)
INSERT INTO staff_permission(staff_user_id, permission_id)
SELECT staff.staff_user_id, dashboard.permission_id
FROM staff_user AS staff CROSS JOIN dashboard
WHERE staff.role = 'admin'
ON CONFLICT DO NOTHING;

DO $settings$
DECLARE
    -- Existing installations can keep their settings in either default schema.
    alternate_schema text := CASE current_schema() WHEN 'voc' THEN 'public' WHEN 'public' THEN 'voc' END;
    reply_table regclass := coalesce(to_regclass('quick_reply'), to_regclass(alternate_schema || '.quick_reply'));
BEGIN
    IF coalesce(to_regclass('staff_assignment_policy'),
                to_regclass(alternate_schema || '.staff_assignment_policy')) IS NULL THEN
        CREATE TABLE IF NOT EXISTS staff_assignment_policy (
            company_id uuid NOT NULL,
            staff_user_id uuid NOT NULL,
            is_enabled boolean NOT NULL,
            batch_size integer NOT NULL CHECK (batch_size > 0),
            refill_mode text NOT NULL CHECK (refill_mode IN ('immediate', 'after_all')),
            PRIMARY KEY (company_id, staff_user_id),
            FOREIGN KEY (company_id, staff_user_id)
                REFERENCES staff_user(company_id, staff_user_id) ON DELETE CASCADE
        );
    END IF;

    IF reply_table IS NULL THEN
        CREATE TABLE IF NOT EXISTS quick_reply (
            reply_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            company_id uuid NOT NULL REFERENCES company ON DELETE CASCADE,
            owner_id uuid,
            team_id uuid,
            domain_id uuid NOT NULL,
            category_id uuid NOT NULL,
            name varchar(80) NOT NULL CHECK (length(trim(name)) > 0),
            body varchar(2000) NOT NULL CHECK (length(trim(body)) > 0),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CHECK (num_nonnulls(owner_id, team_id) = 1),
            FOREIGN KEY (company_id, owner_id) REFERENCES staff_user(company_id, staff_user_id) ON DELETE CASCADE,
            FOREIGN KEY (company_id, team_id) REFERENCES team(company_id, team_id) ON DELETE CASCADE,
            FOREIGN KEY (domain_id, category_id) REFERENCES category(domain_id, category_id)
        );
        reply_table := 'quick_reply'::regclass;
    END IF;
    EXECUTE format('CREATE INDEX IF NOT EXISTS quick_reply_company_owner ON %s(company_id, owner_id) WHERE team_id IS NULL', reply_table);
    EXECUTE format('CREATE INDEX IF NOT EXISTS quick_reply_company_team ON %s(company_id, team_id) WHERE team_id IS NOT NULL', reply_table);

    ALTER TABLE company ADD COLUMN IF NOT EXISTS messenger_provider text CHECK (messenger_provider IN ('slack', 'discord'));

    IF coalesce(to_regclass('messenger_destination'),
                to_regclass(alternate_schema || '.messenger_destination')) IS NULL THEN
        CREATE TABLE IF NOT EXISTS messenger_destination (
            company_id uuid NOT NULL REFERENCES company ON DELETE CASCADE,
            provider text NOT NULL CHECK (provider IN ('slack', 'discord')),
            team_id uuid,
            staff_user_id uuid,
            messenger_channel varchar(100) NOT NULL CHECK (length(trim(messenger_channel)) > 0),
            CHECK (num_nonnulls(team_id, staff_user_id) <= 1),
            UNIQUE NULLS NOT DISTINCT (company_id, provider, team_id, staff_user_id),
            FOREIGN KEY (company_id, team_id) REFERENCES team(company_id, team_id) ON DELETE CASCADE,
            FOREIGN KEY (company_id, staff_user_id) REFERENCES staff_user(company_id, staff_user_id) ON DELETE CASCADE
        );
    END IF;
END $settings$;
