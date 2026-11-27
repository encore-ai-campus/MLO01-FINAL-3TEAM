import os
from contextlib import closing
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

import psycopg2
from django.db import DatabaseError
from django.test import Client, SimpleTestCase
from psycopg2 import sql
from psycopg2.extensions import make_dsn

from apps.preferences import lineage
from tests.database import use_database


class LineageAccessTests(SimpleTestCase):
    def setUp(self):
        self.actor = {"role": "admin", "is_company_admin": True, "company_uuid": str(uuid4()), "visible_sidebar": [], "name": "관리자"}
        self.enterContext(patch("apps.preferences.presentation.lineage.current_actor", return_value=self.actor))
        self.client = Client()

    def test_regular_staff_cannot_open_each_tab(self):
        with patch.object(lineage, "event_trace") as events, patch.object(lineage, "model_history") as models:
            for role in (None, "agent"):
                self.actor["role"] = role
                self.actor["is_company_admin"] = False
                for view in ("flow", "event", "model"):
                    self.assertEqual(self.client.get("/settings/lineage/", {"view": view}).status_code, 403)
            events.assert_not_called()
            models.assert_not_called()

    def test_super_admin_can_select_a_customer_and_selection_survives_navigation(self):
        self.actor["role"] = "super_admin"
        customer = str(uuid4())
        with patch.object(lineage, "companies", return_value=[{"id": customer, "name": "고객사"}]), \
                patch.object(lineage, "event_trace", return_value={"tickets": [], "has_next": True}) as query:
            response = self.client.get("/settings/lineage/", {"view": "event", "company": customer})
        self.assertEqual(response.status_code, 200)
        query.assert_called_once_with(customer, "", 1)
        self.assertContains(response, 'name="company"')
        self.assertContains(response, f'?view=model&amp;company={customer}')
        self.assertContains(response, f'company={customer}&amp;page=2')

    def test_super_admin_must_select_a_known_customer(self):
        self.actor["role"] = "super_admin"
        with patch.object(lineage, "companies", return_value=[]), patch.object(lineage, "event_trace") as query:
            response = self.client.get("/settings/lineage/?view=event")
            self.assertContains(response, "조회할 고객사를 선택해 주세요.")
            for customer in (str(uuid4()), "invalid"):
                self.assertEqual(self.client.get("/settings/lineage/", {
                    "view": "event", "company": customer,
                }).status_code, 400)
            query.assert_not_called()

    def test_company_parameter_cannot_override_session_company(self):
        with patch.object(lineage, "event_trace", return_value={}) as query:
            response = self.client.get("/settings/lineage/", {"view": "event", "q": "#12", "company": str(uuid4())})
        self.assertEqual(response.status_code, 200)
        query.assert_called_once_with(self.actor["company_uuid"], "00000012", 1)

    def test_settings_home_shows_lineage_card_to_both_admin_roles(self):
        with patch("apps.preferences.presentation.views.current_actor", return_value=self.actor):
            for role in ("admin", "super_admin", "agent"):
                self.actor.update(role=role, is_company_admin=role in {"admin", "super_admin"})
                response = self.client.get("/settings/")
                self.assertEqual(response.status_code, 200)
                self.assertEqual('<strong>데이터 리니지</strong>' in response.content.decode(),
                                 role in {"admin", "super_admin"})

    def test_invalid_inputs_do_not_query_data(self):
        with patch.object(lineage, "event_trace") as query:
            for inputs in ({"q": "invalid"}, {"page": "0"}, {"q": "9" * 37}):
                self.assertEqual(self.client.get("/settings/lineage/", {"view": "event", **inputs}).status_code, 400)
            query.assert_not_called()

    def test_existing_layout_and_empty_states_render(self):
        flow = self.client.get("/settings/lineage/")
        self.assertEqual(flow.status_code, 200)
        self.assertContains(flow, '/static/shared/desk.css')
        self.assertContains(flow, '/static/preferences/settings.css')
        self.assertContains(flow, 'class="desk-nav"')
        with patch.object(lineage, "model_history", return_value={"runs": []}):
            self.assertContains(self.client.get("/settings/lineage/?view=model"), "등록된 학습 이력이 없습니다.")
        with patch.object(lineage, "event_trace", side_effect=DatabaseError):
            response = self.client.get("/settings/lineage/?view=event")
        self.assertEqual(response.status_code, 503)
        self.assertIn("리니지 데이터를 조회할 수 없습니다.", response.content.decode())


@skipUnless(os.getenv("VOC_LINEAGE_TEST_DSN"), "Set VOC_LINEAGE_TEST_DSN for isolated PostgreSQL lineage tests")
class LineageDatabaseTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.base_dsn = os.environ["VOC_LINEAGE_TEST_DSN"]
        cls.database = "lineage_test_" + uuid4().hex
        with closing(psycopg2.connect(cls.base_dsn)) as conn:
            conn.autocommit = True
            with conn.cursor() as cursor:
                cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(cls.drop_database)
        cls.dsn = make_dsn(cls.base_dsn, dbname=cls.database)
        cls.company, cls.foreign, cls.event, cls.foreign_event = (str(uuid4()) for _ in range(4))
        with closing(psycopg2.connect(cls.dsn)) as conn, conn.cursor() as c:
            c.execute("""
                CREATE SCHEMA voc;
                CREATE TABLE company(company_uuid uuid, name text, email_domain text);
                CREATE TABLE ticket(ticket_uuid uuid, company_uuid uuid, ticket_number text, title text,
                    created_at timestamptz DEFAULT now(), source_event_id uuid, sentiment_status text, category_status text);
                CREATE TABLE voc.bronze_messages(event_id uuid, company_id text, channel text, connection_id text,
                    source_message_id text, received_at timestamptz DEFAULT now(), raw_payload jsonb DEFAULT '{}');
                CREATE TABLE voc.silver_messages(event_id uuid, schema_version integer, body_version integer,
                    source_channel_id text, standardized_at timestamptz);
                CREATE TABLE voc.ticket_source_connection(company_uuid uuid, company_id text, channel text,
                    connection_id text, source_channel_id text);
                CREATE TABLE voc.feature_snapshot(feature_uuid uuid, company_uuid uuid, silver_event_id uuid,
                    body_version integer, preprocess_version integer, received_at timestamptz);
                CREATE TABLE voc.training_dataset_item(dataset_uuid uuid, feature_uuid uuid);
                CREATE TABLE voc.training_dataset(dataset_uuid uuid, company_uuid uuid, dataset_version text,
                    purpose text, sha256 text, row_count integer, finalized_at timestamptz, created_at timestamptz DEFAULT now());
                CREATE TABLE voc.training_run(workflow_ref text, train_dataset_uuid uuid, eval_dataset_uuid uuid,
                    base_model_uri text, mlflow_run_id text, sentiment_macro_f1 float, category_macro_f1 float);
                CREATE TABLE voc.model_version(workflow_ref text, model_name text, registry_version integer);
            """)
            for company, event, title in ((cls.company, cls.event, "내 회사"), (cls.foreign, cls.foreign_event, "다른 회사")):
                c.execute("INSERT INTO company VALUES (%s,%s,'example.test')", (company, title))
                c.execute("INSERT INTO ticket(ticket_uuid,company_uuid,ticket_number,title,source_event_id) VALUES (%s,%s,'00000012',%s,%s)",
                          (str(uuid4()), company, title, event))
                c.execute("INSERT INTO voc.bronze_messages(event_id,source_message_id,channel) VALUES (%s,%s,'gmail')", (event, title))
                feature, dataset = str(uuid4()), str(uuid4())
                c.execute("INSERT INTO voc.feature_snapshot VALUES (%s,%s,%s,1,1,now())", (feature, company, event))
                c.execute("INSERT INTO voc.training_dataset(dataset_uuid,company_uuid,dataset_version,purpose) VALUES (%s,%s,%s,'train')",
                          (dataset, company, title))
                c.execute("INSERT INTO voc.training_dataset_item VALUES (%s,%s)", (dataset, feature))
                c.execute("INSERT INTO voc.training_run(workflow_ref,train_dataset_uuid,eval_dataset_uuid) VALUES (%s,%s,%s)",
                          (title, dataset, dataset))
            conn.commit()

    @classmethod
    def drop_database(cls):
        with closing(psycopg2.connect(cls.base_dsn)) as conn:
            conn.autocommit = True
            with conn.cursor() as cursor:
                cursor.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(cls.database)))

    def setUp(self):
        self.enterContext(use_database(self.dsn))

    def test_number_collision_and_foreign_event_are_company_scoped(self):
        result = lineage.event_trace(self.company, "00000012", 1)
        self.assertEqual(result["event"]["source_message_id"], "내 회사")
        self.assertEqual([row["dataset_version"] for row in result["datasets"]], ["내 회사"])
        self.assertNotIn("event", lineage.event_trace(self.company, self.foreign_event, 1))
        self.assertEqual([row["title"] for row in lineage.event_trace(self.company, "", 1)["tickets"]], ["내 회사"])
        actor = {"role": "admin", "is_company_admin": True, "company_uuid": self.company, "visible_sidebar": [], "name": "관리자"}
        with patch("apps.preferences.presentation.lineage.current_actor", return_value=actor):
            response = Client().get("/settings/lineage/?view=event&q=12")
        self.assertEqual(response.status_code, 200)
        self.assertIn("내 회사", response.content.decode())
        self.assertNotIn("다른 회사", response.content.decode())

    def test_training_runs_are_company_scoped(self):
        self.assertEqual([row["workflow_ref"] for row in lineage.model_history(self.company, 1)["runs"]], ["내 회사"])
        actor = {"role": "admin", "is_company_admin": True, "company_uuid": self.company, "visible_sidebar": [], "name": "관리자"}
        with patch("apps.preferences.presentation.lineage.current_actor", return_value=actor):
            response = Client().get("/settings/lineage/?view=model")
        self.assertEqual(response.status_code, 200)
        self.assertIn("내 회사", response.content.decode())
        self.assertNotIn("다른 회사", response.content.decode())

    def test_super_admin_can_view_each_customer_without_mixing_results(self):
        self.assertEqual({row["id"] for row in lineage.companies()}, {self.company, self.foreign})
        actor = {"role": "super_admin", "company_uuid": self.company, "visible_sidebar": [], "name": "운영자"}
        with patch("apps.preferences.presentation.lineage.current_actor", return_value=actor):
            for company, own, other in ((self.company, "내 회사", "다른 회사"), (self.foreign, "다른 회사", "내 회사")):
                response = Client().get("/settings/lineage/", {"view": "model", "company": company})
                self.assertEqual(response.status_code, 200)
                # Both company names belong in the selector, but only the selected company's run is rendered.
                self.assertIn(f'<summary>{own}', response.content.decode())
                self.assertNotIn(f'<summary>{other}', response.content.decode())
