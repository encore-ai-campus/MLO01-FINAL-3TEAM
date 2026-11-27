import json
import os
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, skipUnless
from unittest.mock import patch
from urllib.error import HTTPError
from uuid import UUID, uuid4

import psycopg2
from django.test import Client, override_settings
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

    def test_settings_load_styles_and_show_only_allowed_navigation(self):
        pages = ("/settings/", "/settings/display/", "/settings/replies/", "/settings/messenger/",
                 "/settings/permissions/", "/settings/assignment/")
        for path in pages:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertContains(response, "/static/preferences/settings.css")
                self.assertContains(response, 'href="/settings/#hr"')
        employee = self.session(self.agent).get("/settings/")
        self.assertContains(employee, 'href="/settings/#personal"')
        self.assertNotContains(employee, 'href="/settings/#hr"')
        self.assertNotContains(employee, 'href="/settings/assignment/"')
        self.assertNotContains(self.client.get("/home/"), "/static/preferences/settings.css")

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

    def test_admin_cannot_list_open_or_save_super_admin_permissions(self):
        operator = self.make_staff("Operator", "inside.test", self.company, None, "super_admin")
        owner = self.session(operator)
        path = f"/settings/permissions/{operator}/"
        grant = {"permissions": ["analytics.read"]}
        self.assertEqual(owner.post(path, grant).status_code, 302)

        listing = self.client.get("/settings/permissions/")
        self.assertNotContains(listing, "operator@inside.test")
        self.assertNotContains(listing, path)
        for staff in (self.admin, self.agent):
            self.assertContains(listing, f"/settings/permissions/{staff}/")
        missing = self.client.get(f"/settings/permissions/{uuid4()}/")
        for response in (self.client.get(path), self.client.post(path, {"permissions": ["chat.handle"]})):
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.content, missing.content)
        self.assertEqual(owner.get(path).context["form"].initial["permissions"], grant["permissions"])

    def test_super_admin_can_list_and_edit_all_company_roles(self):
        operator = self.make_staff("Operator", "inside.test", self.company, None, "super_admin")
        peer = self.make_staff("Peer", "inside.test", self.company, None, "super_admin")
        owner = self.session(operator)
        self.assertContains(owner.get("/settings/"), 'href="/settings/permissions/"')
        listing = owner.get("/settings/permissions/")
        grants = ["analytics.read", "chat.handle", "quick_reply.team.edit", "ticket.handle"]
        for staff in (operator, peer, self.admin, self.agent):
            with self.subTest(staff=staff):
                path = f"/settings/permissions/{staff}/"
                self.assertContains(listing, path)
                self.assertEqual(owner.post(path, {"permissions": grants}).status_code, 302)
                self.assertEqual(owner.get(path).context["form"].initial["permissions"], grants)
                self.assertEqual(owner.post(path, {}).status_code, 302)
                self.assertEqual(owner.get(path).context["form"].initial["permissions"], [])

        foreign = f"/settings/permissions/{self.foreign}/"
        self.assertNotContains(listing, foreign)
        self.assertEqual(owner.get(foreign).status_code, 404)
        self.assertEqual(owner.post(foreign, {}).status_code, 404)

    @override_settings(MLFLOW_TRACKING_URI="http://mlflow.test", ARGO_WORKFLOWS_URL="http://argo.test")
    def test_training_form_uses_company_choices_and_reuses_workflow_on_post_retry(self):
        page = "/model-training/"
        super_admin = self.make_staff("Operator", "inside.test", self.company, self.team, "super_admin")
        client = self.session(super_admin)
        self.assertEqual(self.session(self.agent).get(page).status_code, 403)
        self.assertEqual(self.client.post(page, {}).status_code, 403)
        self.assertEqual(self.session(super_admin, csrf=True).post(page, {}).status_code, 403)
        token_file = Path(self.enterContext(TemporaryDirectory())) / "argo-token"
        self.enterContext(override_settings(ARGO_WORKFLOWS_TOKEN="", ARGO_WORKFLOWS_TOKEN_FILE=token_file))
        token_file.write_text("expired", encoding="utf-8")

        def version(name, role, company=None):
            tags = [{"key": "voc_training_input", "value": role}]
            if role == "encoder":
                tags.append({"key": "source_revision", "value": "77c8b3d707df785034b4e50f2da5d37be5f0f546"})
            if company:
                tags.append({"key": "company_uuid", "value": company})
            return {"name": name, "version": "1", "status": "READY", "tags": tags}

        models = {"model_versions": [
            version("base", "encoder"), version("company", "multitask", self.company),
            version("foreign", "multitask", self.other_company), version("legacy", "legacy"), version("missing", "encoder"),
        ]}
        created = {}
        submissions = []
        tokens = []

        def request(url, token, data=None):
            if "/get-download-uri?" in url:
                return {"artifact_uri": "s3://models/missing" if "name=missing" in url else "s3://models/ready"}
            if url.startswith("http://mlflow.test/"):
                return models
            tokens.append(token)
            if token == "expired":
                raise HTTPError(url, 401, "Unauthorized", {}, None)
            if data is None:
                return created
            options = data["submitOptions"]
            submissions.append(options)
            if created:
                raise HTTPError(url, 409, "Already exists", {}, None)
            created.update({
                "metadata": {"name": options["name"]},
                "spec": {"arguments": {"parameters": [
                    {"name": key, "value": value}
                    for key, value in (parameter.split("=", 1) for parameter in options["parameters"])
                ]}},
            })
            return created

        with patch("apps.preferences.training_inputs.model_files_available", side_effect=lambda uri, role: uri.endswith("/ready")), patch("apps.preferences.training.request_json", side_effect=request), patch(
            "apps.preferences.presentation.training.evaluation_sets", return_value=[("eval-v1", "eval-v1 (1건)")],
        ) as datasets:
            opened = client.get(page)
            datasets.assert_called_with(UUID(self.company))
            self.assertContains(opened, "models:/base/1")
            self.assertContains(opened, "models:/company/1")
            self.assertNotContains(opened, "models:/foreign/1")
            self.assertNotContains(opened, "models:/legacy/1")
            self.assertNotContains(opened, "models:/missing/1")
            params = {"request_id": str(uuid4()), "as_of_at": "2025-11-20T10:05",
                      "model": "models:/base/1", "eval_dataset": "eval-v1"}
            for forged in ({"model": "models:/foreign/1"}, {"eval_dataset": "eval-v2"}):
                self.assertEqual(client.post(page, {**params, **forged}).status_code, 400)
            self.assertEqual(submissions, [])
            rejected = client.post(page, params)
            self.assertEqual(rejected.status_code, 503)
            self.assertIn("Argo 제출 인증에 실패했습니다.", rejected.text)
            token_file.write_text("fresh", encoding="utf-8")
            success = client.post(page, {**params, "company": self.other_company})
            self.assertEqual(success.status_code, 303)
            self.assertEqual(success.url, f"/model-training/runs/{created['metadata']['name']}/")
            token_file.write_text("rotated", encoding="utf-8")
            retried = client.post("/settings/model-training/", params)
            self.assertEqual(retried.status_code, 303)
            self.assertEqual(retried.url, success.url)
            self.assertEqual(tokens, ["expired", "fresh", "rotated", "rotated"])
            self.assertEqual(submissions[0], submissions[1])
            self.assertIn(f"company={self.company}", submissions[0]["parameters"])
            self.assertIn("label-cutoff=2025-11-20T10:05:00+09:00", submissions[0]["parameters"])

    def test_training_menu_and_legacy_address_require_super_admin(self):
        operator = self.make_staff("Operator", "inside.test", self.company, self.team, "super_admin")
        client = self.session(operator)
        page = client.get("/settings/").text
        self.assertLess(page.index('href="/model-training/"'), page.index('<details class="desk-settings-menu"'))
        self.assertNotContains(self.client.get("/settings/"), 'href="/model-training/"')
        self.assertNotContains(self.session(self.agent).get("/settings/"), 'href="/model-training/"')
        self.assertEqual(client.get("/settings/model-training/").url, "/model-training/")
        self.assertEqual(self.client.get("/settings/model-training/").status_code, 403)

    @override_settings(MLFLOW_TRACKING_URI="http://mlflow.test", ARGO_WORKFLOWS_URL="http://argo.test")
    def test_retry_after_lost_argo_response_reuses_the_accepted_workflow(self):
        operator = self.make_staff("Operator", "inside.test", self.company, self.team, "super_admin")
        client = self.session(operator)
        form = {"request_id": str(uuid4()), "as_of_at": "2025-11-20T10:05",
                "model": "models:/base/1", "eval_dataset": "eval-v1"}
        accepted = {}

        def argo(path, payload=None):
            if path == "/submit":
                options = payload["submitOptions"]
                if accepted:
                    self.assertEqual(options["name"], accepted["metadata"]["name"])
                    raise HTTPError(path, 409, "Already exists", {}, None)
                accepted.update({
                    "metadata": {"name": options["name"]},
                    "spec": {"arguments": {"parameters": [
                        {"name": key, "value": value}
                        for key, value in (param.split("=", 1) for param in options["parameters"])
                    ]}},
                })
                raise TimeoutError("Argo accepted the workflow before the connection timed out")
            self.assertEqual(path, f"/{accepted['metadata']['name']}")
            return accepted

        with patch("apps.preferences.training.available_models", return_value=[("models:/base/1", "Base")]), patch(
            "apps.preferences.presentation.training.evaluation_sets", return_value=[("eval-v1", "eval-v1")],
        ), patch("apps.preferences.training.argo_request", side_effect=argo):
            self.assertEqual(client.post("/model-training/", form).status_code, 503)
            retry = client.post("/model-training/", form)
            self.assertEqual(retry.status_code, 303)
            self.assertEqual(retry.url, f"/model-training/runs/{accepted['metadata']['name']}/")
            changed = client.post("/model-training/", {**form, "as_of_at": "2025-11-20T10:06"})
            self.assertEqual(changed.status_code, 503)
            self.assertNotIn("Location", changed.headers)

    def test_revoking_super_admin_blocks_the_existing_session(self):
        operator = self.make_staff("Operator", "inside.test", self.company, self.team, "super_admin")
        client = self.session(operator)
        name = "voc-training-" + uuid4().hex
        self.assertContains(client.get("/settings/"), 'href="/model-training/"')
        self.cursor.execute("UPDATE staff_user SET role='admin' WHERE staff_user_uuid=%s", (operator,))
        self.assertNotContains(client.get("/settings/"), 'href="/model-training/"')
        with patch("apps.preferences.training.argo_request") as argo:
            for path in ("/model-training/", "/model-training/runs/",
                         f"/model-training/runs/{name}/", f"/model-training/runs/{name}/status/"):
                with self.subTest(path=path):
                    self.assertEqual(client.get(path).status_code, 403)
            self.assertEqual(client.post("/model-training/", {}).status_code, 403)
            argo.assert_not_called()

    @override_settings(ARGO_WORKFLOWS_UI_URL="http://argo.test", MINIO_CONSOLE_URL="http://minio.test")
    def test_training_history_and_detail_only_show_company_web_runs(self):
        operator = self.make_staff("Operator", "inside.test", self.company, self.team, "super_admin")
        client = self.session(operator)
        other_operator = self.make_staff("Operator", "outside.test", self.other_company, self.other_team, "super_admin")

        def workflow(index, company=self.company, desk=True):
            return {
                "metadata": {"name": f"voc-training-{index:032x}", "uid": f"run-{index}",
                             "labels": {"voc.company": company, **({"voc.requested-by": operator} if desk else {})},
                             "creationTimestamp": f"2026-11-{index:02d}T00:00:00Z"},
                "spec": {"workflowTemplateRef": {"name": "voc-training"}, "arguments": {"parameters": [
                    {"name": "company", "value": company}, {"name": "eval-dataset", "value": "eval-v1"},
                ]}},
                "status": {"phase": "Succeeded"},
            }

        own, foreign = workflow(12), workflow(13, self.other_company)
        status_path = f"/model-training/runs/{own['metadata']['name']}/status/"
        with patch("apps.preferences.training.argo_request") as argo, patch(
            "apps.preferences.training.progress_and_result", return_value=([], {"train_version": "train-v1"}),
        ) as data:
            argo.side_effect = [
                {"items": [workflow(i) for i in range(1, 8)], "metadata": {"continue": "page-2"}},
                {"items": [*map(workflow, range(8, 13)), foreign, workflow(14, desk=False)]},
            ]
            listing = client.get("/model-training/runs/").json()["runs"]
            self.assertEqual(len(listing), 10)
            self.assertEqual(listing[0]["name"], own["metadata"]["name"])
            self.assertEqual(argo.call_args.kwargs["query"]["listOptions.continue"], "page-2")
            argo.side_effect = None
            argo.return_value = workflow(12, self.other_company)
            self.assertEqual(client.get(status_path).status_code, 404)
            self.assertEqual(self.client.get(status_path).status_code, 403)
            argo.return_value = own
            self.assertEqual(self.session(other_operator).get(status_path).status_code, 404)
            data.assert_not_called()
            summary = client.get(status_path).json()
            self.assertEqual(summary["result"]["train_version"], "train-v1")
            self.assertIn("MinIO 콘솔", summary["links"])

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

    def test_analytics_menu_follows_work_permission_and_saved_preference(self):
        agent = self.session(self.agent)
        for staff, client in ((self.admin, self.client), (self.agent, agent)):
            for granted in (True, False):
                codes = ["analytics.read"] if granted else []
                self.assertEqual(self.change_rights(staff, *codes).status_code, 302)
                for saved in (None, ["tickets", "analytics"], []):
                    with self.subTest(staff=staff, granted=granted, sidebar=saved):
                        preferences = {} if saved is None else {"sidebar": saved}
                        self.cursor.execute("UPDATE staff_user SET display_preferences=%s WHERE staff_user_uuid=%s",
                                            (json.dumps(preferences), staff))
                        visible = granted and (saved is None or "analytics" in saved)
                        display = client.get("/settings/display/")
                        self.assertEqual(display.status_code, 200)
                        self.assertEqual('value="analytics"' in display.content.decode(), granted)
                        home = client.get("/home/")
                        self.assertEqual(home.status_code, 200)
                        self.assertEqual('href="/analytics/"' in home.content.decode(), visible)

    def test_display_preferences_are_saved_per_employee(self):
        agent = self.session(self.agent)
        page = agent.get("/settings/display/")
        self.assertContains(page, "카드 표시 및 순서")
        self.assertContains(page, "좌측 패널 표시 설정")
        cards = [{"id": "requests", "visible": False}, {"id": "tickets", "visible": True},
                 {"id": "performance", "visible": True}]
        response = agent.post("/settings/display/", {"sidebar": ["home", "analytics"], "cards": json.dumps(cards)})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(agent.post("/settings/display/", {"sidebar": ["tickets"], "cards": "[]"}).status_code, 400)
        self.assertEqual(agent.post("/settings/display/cards/", {"cards": "[]"}).status_code, 400)
        reopened = self.session(self.agent).get("/home/")
        self.assertNotContains(reopened, 'class="desk-nav-item" href="/tickets/"')
        self.assertContains(reopened, '"id": "requests", "visible": false')
        self.assertContains(self.client.get("/home/"), 'class="desk-nav-item" href="/tickets/"')
        self.assertContains(agent.get("/settings/display/"), 'data-card="requests"')
        self.assertContains(agent.get("/home/"), 'href="/settings/display/?from=home"')
        saved_from_home = agent.post("/settings/display/?from=home", {
            "sidebar": ["home", "analytics"], "cards": json.dumps(cards),
        })
        self.assertEqual(saved_from_home["Location"], "/home/")

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
