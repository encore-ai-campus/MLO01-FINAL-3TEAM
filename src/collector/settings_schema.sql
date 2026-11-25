ALTER TABLE staff_user ADD COLUMN IF NOT EXISTS role text;
ALTER TABLE staff_user ADD COLUMN IF NOT EXISTS display_preferences jsonb NOT NULL DEFAULT '{}';

INSERT INTO permission(code, name) VALUES
    ('ticket.handle', '티켓 상담'),
    ('chat.handle', '채팅 상담'),
    ('quick_reply.team.edit', '팀별 신속답변 편집')
ON CONFLICT (code) DO NOTHING;

-- Preserve existing dashboard access once. Re-running schema setup must respect revocations.
WITH dashboard AS (
    INSERT INTO permission(code, name) VALUES ('analytics.read', 'VoC 대시보드 접근')
    ON CONFLICT (code) DO NOTHING
    RETURNING permission_uuid
)
INSERT INTO staff_permission(staff_user_uuid, permission_uuid)
SELECT staff.staff_user_uuid, dashboard.permission_uuid
FROM staff_user AS staff CROSS JOIN dashboard
WHERE staff.role = 'admin'
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS staff_assignment_policy (
    company_uuid uuid NOT NULL,
    staff_user_uuid uuid NOT NULL,
    enabled boolean NOT NULL,
    batch_size integer NOT NULL CHECK (batch_size > 0),
    refill_mode text NOT NULL CHECK (refill_mode IN ('immediate', 'after_all')),
    PRIMARY KEY (company_uuid, staff_user_uuid),
    FOREIGN KEY (company_uuid, staff_user_uuid)
        REFERENCES staff_user(company_uuid, staff_user_uuid) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS quick_reply (
    reply_uuid uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_uuid uuid NOT NULL REFERENCES company ON DELETE CASCADE,
    owner_uuid uuid,
    team_uuid uuid,
    domain_uuid uuid NOT NULL,
    category_uuid uuid NOT NULL,
    name varchar(80) NOT NULL CHECK (length(trim(name)) > 0),
    body varchar(2000) NOT NULL CHECK (length(trim(body)) > 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(owner_uuid, team_uuid) = 1),
    FOREIGN KEY (company_uuid, owner_uuid) REFERENCES staff_user(company_uuid, staff_user_uuid) ON DELETE CASCADE,
    FOREIGN KEY (company_uuid, team_uuid) REFERENCES team(company_uuid, team_uuid) ON DELETE CASCADE,
    FOREIGN KEY (domain_uuid, category_uuid) REFERENCES category(domain_uuid, category_uuid)
);
CREATE INDEX IF NOT EXISTS quick_reply_company_owner ON quick_reply(company_uuid, owner_uuid) WHERE team_uuid IS NULL;
CREATE INDEX IF NOT EXISTS quick_reply_company_team ON quick_reply(company_uuid, team_uuid) WHERE team_uuid IS NOT NULL;

ALTER TABLE company ADD COLUMN IF NOT EXISTS messenger_provider text CHECK (messenger_provider IN ('slack', 'discord'));

CREATE TABLE IF NOT EXISTS messenger_destination (
    company_uuid uuid NOT NULL REFERENCES company ON DELETE CASCADE,
    provider text NOT NULL CHECK (provider IN ('slack', 'discord')),
    team_uuid uuid,
    staff_user_uuid uuid,
    channel varchar(100) NOT NULL CHECK (length(trim(channel)) > 0),
    CHECK (num_nonnulls(team_uuid, staff_user_uuid) <= 1),
    UNIQUE NULLS NOT DISTINCT (company_uuid, provider, team_uuid, staff_user_uuid),
    FOREIGN KEY (company_uuid, team_uuid) REFERENCES team(company_uuid, team_uuid) ON DELETE CASCADE,
    FOREIGN KEY (company_uuid, staff_user_uuid) REFERENCES staff_user(company_uuid, staff_user_uuid) ON DELETE CASCADE
);
