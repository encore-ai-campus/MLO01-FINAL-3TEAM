import json
import os
from contextlib import closing
from pathlib import Path
from unittest import TestCase, skipUnless
from uuid import uuid4

import psycopg2
from django.test import Client
from psycopg2 import sql
from psycopg2.extensions import make_dsn

from apps.accounts.repository import token_digest
from tests.database import use_database


@skipUnless(os.getenv("VOC_TASK9_TEST_DSN"), "Set VOC_TASK9_TEST_DSN for PostgreSQL settings tests")
class SettingsTests(TestCase):
    def setUp(self):
        base_dsn = os.environ["VOC_TASK9_TEST_DSN"]
        schema = "settings_test_" + uuid4().hex
        self.addCleanup(self.drop_schema, base_dsn, schema)
        with closing(psycopg2.connect(base_dsn)) as conn, conn.cursor() as cursor:
            conn.autocommit = True
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        dsn = make_dsn(base_dsn, options=f"-c search_path={schema}")
        conn = psycopg2.connect(dsn)
        conn.autocommit = True
        self.addCleanup(conn.close)
        self.cursor = self.enterContext(conn.cursor())
        schema_file = Path(__file__).resolve().parents[2] / "collector" / "ticket_schema.sql"
        foundation = schema_file.read_text(encoding="utf-8").split("CREATE TABLE IF NOT EXISTS public.customer")[0]
        self.cursor.execute(foundation.replace("CREATE EXTENSION IF NOT EXISTS vector;", "").replace("public.", ""))

        self.domain, self.category, self.other_category = (str(uuid4()) for _ in range(3))
        other_domain = str(uuid4())
        for domain in (self.domain, other_domain):
            self.cursor.execute("INSERT INTO domain VALUES (%s,'테스트 도메인')", (domain,))
        for category, domain in ((self.category, self.domain), (self.other_category, other_domain)):
            self.cursor.execute("INSERT INTO category VALUES (%s,%s,'배송')", (category, domain))
        self.company, self.team = self.make_company("inside.test", self.domain)
        self.other_company, self.other_team = self.make_company("outside.test", other_domain)
        self.cursor.execute("ALTER TABLE staff_user ADD COLUMN role text")
        self.admin = self.make_staff("Admin", "inside.test", self.company, self.team, "admin")
        self.agent = self.make_staff("Agent", "inside.test", self.company, self.team)
        self.foreign = self.make_staff("Foreign", "outside.test", self.other_company, self.other_team)
        self.settings_sql = schema_file.with_name("settings_schema.sql")
        if self.settings_sql.exists():
            self.cursor.execute(self.settings_sql.read_text(encoding="utf-8"))
        self.enterContext(use_database(dsn))
        self.client = self.session(self.admin)

    @staticmethod
    def drop_schema(dsn, schema):
        with closing(psycopg2.connect(dsn)) as conn, conn.cursor() as cursor:
            conn.autocommit = True
            cursor.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    def make_company(self, email_domain, domain):
        company, team = str(uuid4()), str(uuid4())
        self.cursor.execute("INSERT INTO company(company_uuid,email_domain,domain_uuid) VALUES (%s,%s,%s)",
                            (company, email_domain, domain))
        self.cursor.execute("INSERT INTO team(team_uuid,company_uuid,name) VALUES (%s,%s,%s)",
                            (team, company, email_domain))
        return company, team

    def make_staff(self, name, domain, company, team, role=None):
        staff = str(uuid4())
        self.cursor.execute("""
            INSERT INTO staff_user(staff_user_uuid, company_uuid, team_uuid, email, name, password_hash, role)
            VALUES (%s,%s,%s,%s,%s,'unused',%s)
        """, (staff, company, team, f"{name.lower()}@{domain}", name, role))
        return staff

    def session(self, staff, csrf=False):
        token = uuid4().hex
        self.cursor.execute("INSERT INTO staff_session VALUES (%s,%s,now()+interval '1 hour')",
                            (token_digest(token), staff))
        client = Client(enforce_csrf_checks=csrf)
        client.cookies["voc_desk_session"] = token
        return client

    def change_rights(self, staff, *codes):
        return self.client.post(f"/settings/permissions/{staff}/", {"permissions": codes})

    def assertContains(self, response, text):
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn(text, response.content.decode())

    def assertNotContains(self, response, text):
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn(text, response.content.decode())

    def team_reply(self, client, scope="team", category=None):
        return client.post("/settings/replies/new/", {
            "name": "배송 안내", "category_uuid": category or self.category, "scope": scope, "body": "확인해 드리겠습니다.",
        })

    def test_only_company_admin_can_manage_staff_permissions(self):
        listing = self.client.get("/settings/permissions/")
        self.assertEqual(listing.status_code, 200)
        self.assertContains(listing, "Agent")
        self.assertNotContains(listing, "Foreign")
        self.assertEqual(self.change_rights(self.foreign, "ticket.handle").status_code, 404)
        self.assertEqual(self.change_rights(self.agent, "ticket.read").status_code, 400)
        agent = self.session(self.agent)
        self.assertEqual(agent.post(f"/settings/permissions/{self.agent}/", {}).status_code, 403)
        self.assertEqual(self.session(self.admin, csrf=True).post(
            f"/settings/permissions/{self.agent}/", {}).status_code, 403)

    def test_independent_permission_changes_take_effect_for_current_sessions(self):
        self.cursor.execute("""
            INSERT INTO staff_permission SELECT %s, permission_uuid FROM permission WHERE code='ticket.read'
        """, (self.agent,))
        self.assertEqual(self.change_rights(self.agent, "analytics.read").status_code, 302)
        response = self.client.get(f"/settings/permissions/{self.agent}/")
        self.assertEqual(response.context["form"].initial["permissions"], ["analytics.read"])
        agent = self.session(self.agent)
        self.assertEqual(self.team_reply(agent).status_code, 403)
        self.assertEqual(self.change_rights(self.agent, "quick_reply.team.edit").status_code, 302)
        self.assertEqual(self.team_reply(agent).status_code, 302)
        self.assertEqual(agent.get("/analytics/").status_code, 403)
        self.cursor.execute("""
            SELECT code FROM staff_permission JOIN permission USING(permission_uuid)
            WHERE staff_user_uuid=%s ORDER BY code
        """, (self.agent,))
        self.assertEqual(self.cursor.fetchall(), [("quick_reply.team.edit",), ("ticket.read",)])
        self.assertEqual(self.change_rights(self.admin).status_code, 302)
        self.cursor.execute(self.settings_sql.read_text(encoding="utf-8"))
        self.assertEqual(self.client.get("/analytics/").status_code, 403)
        self.assertEqual(self.client.get("/settings/permissions/").status_code, 200)

    def test_incomplete_permission_catalog_rejects_save(self):
        self.cursor.execute("DELETE FROM permission WHERE code='chat.handle'")
        self.assertEqual(self.change_rights(self.agent, "ticket.handle").status_code, 503)
        self.cursor.execute("SELECT 1 FROM staff_permission WHERE staff_user_uuid=%s", (self.agent,))
        self.assertIsNone(self.cursor.fetchone())

    def test_assignment_policy_is_saved_for_task10_and_scoped_to_company(self):
        from apps.assignment.services import get_policy
        from django.db import connection

        payload = {"enabled": "on", "batch_size": 5, "refill_mode": "after_all"}
        self.assertEqual(self.client.post(f"/settings/assignment/{self.agent}/", payload).status_code, 302)
        self.assertEqual(self.client.post(f"/settings/assignment/{self.foreign}/", payload).status_code, 404)
        self.assertEqual(self.session(self.agent).get("/settings/assignment/").status_code, 403)
        self.assertEqual(self.client.post(f"/settings/assignment/{self.agent}/",
                                          {**payload, "batch_size": 0}).status_code, 400)
        self.assertEqual(self.client.post(f"/settings/assignment/{self.agent}/",
                                          {"batch_size": 5, "refill_mode": "after_all"}).status_code, 302)
        with connection.cursor() as cursor:
            self.assertEqual(get_policy(cursor, self.company, self.agent), {
                "enabled": False, "batch_size": 5, "refill_mode": "after_all",
            })
            self.assertEqual(get_policy(cursor, self.company, self.admin), {
                "enabled": False, "batch_size": 1, "refill_mode": "after_all",
            })
            self.assertIsNone(get_policy(cursor, self.company, self.foreign))

    def test_reply_visibility_category_and_edit_are_scoped(self):
        agent = self.session(self.agent)
        self.assertEqual(self.team_reply(agent, "personal", self.other_category).status_code, 400)
        personal = self.team_reply(agent, "personal")
        self.assertEqual(personal.status_code, 302)
        self.assertNotContains(self.client.get("/settings/replies/"), "배송 안내")
        self.assertEqual(self.client.get(personal["Location"]).status_code, 404)
        edit = agent.post(personal["Location"], {
            "name": "배송 확인", "category_uuid": self.category, "body": "배송 상태를 확인했습니다.",
        })
        self.assertEqual(edit.status_code, 302)
        self.assertContains(agent.get("/settings/replies/"), "배송 확인")
        self.assertEqual(self.change_rights(self.agent, "quick_reply.team.edit").status_code, 302)
        shared = self.team_reply(agent)
        self.assertEqual(shared.status_code, 302)
        self.assertContains(self.client.get("/settings/replies/", {"scope": "team", "q": "배송",
                                                                 "category": self.category}), "배송 안내")
        self.assertNotContains(self.client.get("/settings/replies/", {"scope": "team", "q": "결제"}), "배송 안내")
        self.assertEqual(self.client.post(shared["Location"], {"action": "delete"}).status_code, 403)
        self.assertEqual(agent.post(shared["Location"], {"action": "delete"}).status_code, 302)

    def test_team_reply_belongs_to_team_after_employee_leaves(self):
        self.assertEqual(self.change_rights(self.agent, "quick_reply.team.edit").status_code, 302)
        author = self.session(self.agent)
        self.assertEqual(self.team_reply(author).status_code, 302)
        self.cursor.execute("DELETE FROM staff_user WHERE staff_user_uuid=%s", (self.agent,))
        self.assertContains(self.client.get("/settings/replies/", {"scope": "team"}), "배송 안내")

    def test_display_preferences_are_saved_per_employee(self):
        agent = self.session(self.agent)
        self.assertEqual(agent.post("/settings/display/", {"sidebar": ["home", "analytics"]}).status_code, 302)
        cards = [{"id": "requests", "visible": False}, {"id": "tickets", "visible": True},
                 {"id": "performance", "visible": True}]
        self.assertEqual(agent.post("/settings/display/cards/", {"cards": json.dumps(cards)}).json(), {"cards": cards})
        self.assertEqual(agent.post("/settings/display/cards/", {"cards": "[]"}).status_code, 400)
        reopened = self.session(self.agent).get("/home/")
        self.assertNotContains(reopened, 'class="desk-nav-item" href="/tickets/"')
        self.assertContains(reopened, '"id": "requests", "visible": false')
        self.assertContains(self.client.get("/home/"), 'class="desk-nav-item" href="/tickets/"')

    def test_messenger_settings_keep_provider_routes_and_scope_employee_and_team(self):
        self.assertEqual(self.session(self.agent).get("/settings/messenger/").status_code, 403)
        self.assertEqual(self.client.post("/settings/messenger/", {
            "provider": "slack", "channel": "#transfer", "destination": "default",
        }).status_code, 302)
        for destination, own, other in (("team", self.team, self.other_team), ("staff", self.agent, self.foreign)):
            for target, expected in ((own, 302), (other, 400)):
                self.assertEqual(self.client.post("/settings/messenger/", {
                    "provider": "slack", "channel": f"#{destination}", "destination": destination, "target": target,
                }).status_code, expected)
        self.assertContains(self.client.get("/settings/messenger/"), "#transfer")
        self.assertContains(self.client.get("/settings/messenger/"), "#team")
        self.assertContains(self.client.get("/settings/messenger/"), "#staff")
        self.assertEqual(self.client.post("/settings/messenger/", {
            "provider": "discord", "channel": "#discord-transfer", "destination": "default",
        }).status_code, 302)
        self.assertNotContains(self.client.get("/settings/messenger/"), "#team")
        self.assertContains(self.client.get("/settings/messenger/?provider=slack"), "#team")
