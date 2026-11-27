"""Test the API against the real PostgreSQL schema in a newly created disposable database."""
import json
import os
import re
import unittest
from base64 import b64decode
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import psycopg2
from argon2 import PasswordHasher
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from tests.database import use_database
from psycopg2 import sql
from psycopg2.extensions import make_dsn

SRC = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get("VOC_TICKET_TEST_ADMIN_DSN"), "Set VOC_TICKET_TEST_ADMIN_DSN for PostgreSQL tests")
class TicketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        admin_dsn = os.environ["VOC_TICKET_TEST_ADMIN_DSN"]
        database = "ticket_api_test_" + uuid4().hex
        with closing(psycopg2.connect(admin_dsn)) as admin, admin.cursor() as cursor:
            admin.autocommit = True
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        cls.addClassCleanup(cls.drop_db, admin_dsn, database)
        cls.dsn = make_dsn(admin_dsn, dbname=database)
        cls.cn = psycopg2.connect(cls.dsn)
        cls.cn.autocommit = True
        cls.addClassCleanup(cls.cn.close)
        cls.cur = cls.cn.cursor()
        cls.addClassCleanup(cls.cur.close)
        for filename in ("schema.sql", "ticket_schema.sql", "training_schema.sql", "settings_schema.sql", "followup_schema.sql"):
            cls.cur.execute((SRC / "collector" / filename).read_text(encoding="utf-8"))
        password_hash = PasswordHasher().hash("SamplePass1!")
        cls.employees = []
        cls.tickets = []
        for i in (1, 2):
            tenant = f"tenant-{i}"
            company, domain, employee = (str(uuid4()) for _ in range(3))
            cls.cur.execute("INSERT INTO domain(domain_uuid,name) VALUES (%s,%s)", (domain, tenant))
            cls.cur.execute("INSERT INTO company(company_uuid,email_domain,domain_uuid) VALUES (%s,%s,%s)",
                            (company, f"qa{i}.test", domain))
            cls.cur.execute("INSERT INTO staff_user(staff_user_uuid,company_uuid,email,name,password_hash) "
                            "VALUES (%s,%s,%s,'시험 상담사',%s)", (employee, company, f"agent1@qa{i}.test", password_hash))
            cls.cur.execute("INSERT INTO staff_permission SELECT %s,permission_uuid FROM permission WHERE code='ticket.read'",
                            (employee,))
            cls.employees.append(employee)
            if i == 1:
                cls.cur.execute("INSERT INTO team(company_uuid,name) VALUES (%s,'배송팀') RETURNING team_uuid", (company,))
                cls.team = str(cls.cur.fetchone()[0])
            for ch in ("gmail", "discord"):
                cls.cur.execute("INSERT INTO channel(company_uuid,type,name) VALUES (%s,%s,%s) RETURNING channel_uuid",
                                (company, ch, ch))
                channel = cls.cur.fetchone()[0]
                room = "101" if ch == "discord" else ""
                cls.cur.execute("INSERT INTO voc.ticket_source_connection VALUES(%s,%s,%s,%s,%s,%s)",
                                (tenant, ch, "account", room, company, channel))
                message_count = (2 if i == 1 else 11) if ch == "gmail" else 1
                for num in range(1, message_count + 1):
                    ident = str(uuid4())
                    moment = f"2024-02-{num:02}T16:00:00Z"
                    cls.cur.execute("INSERT INTO voc.bronze_messages VALUES(%s,%s,%s,'account',%s,'qa',%s,'{}','{}')",
                                    (ident, tenant, ch, str(num), moment))
                    cls.cur.execute("""
                        INSERT INTO voc.silver_messages(schema_version,event_id,company_id,channel,connection_id,
                            source_message_id,source_conversation_id,source_channel_id,message_kind,sender_ref,
                            body_text,body_version,received_at,source_metadata)
                        VALUES (4,%s,%s,%s,'account',%s,'thread',%s,'customer_inquiry','customer_10',%s,1,%s,%s)
                        """, (ident, tenant, ch, str(num), room or None, '배송 요청 '+str(num), moment,
                              '{}' if room else '{"label_ids":[],"to_addresses":[]}'))
                    cls.cur.execute("SELECT ticket_uuid FROM ticket WHERE source_event_id=%s", (ident,))
                    cls.tickets.append(str(cls.cur.fetchone()[0]))

        a, b, discord, *_ = cls.tickets
        cls.cur.execute("UPDATE ticket SET assigned_team_uuid=%s WHERE ticket_uuid IN (%s,%s)", (cls.team,a,b))
        cls.cur.execute("""
            UPDATE ticket SET assignee_uuid=%s,status='보류',sentiment='부정',sentiment_status='succeeded'
            WHERE ticket_uuid=%s""", (cls.employees[0],b))
        cls.cur.execute("INSERT INTO ticket_duplicate_candidate(ticket_uuid,candidate_ticket_uuid) VALUES (%s,%s)",
                        sorted((a,discord)))
        cls.enterClassContext(use_database(cls.dsn))

    @staticmethod
    def drop_db(dsn, name):
        with closing(psycopg2.connect(dsn)) as admin, admin.cursor() as cursor:
            admin.autocommit = True
            cursor.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))

    def setUp(self):
        self.client = Client()

    def test_followup_tracking_notifications_and_contact_completion(self):
        base = "/api/follow-up"
        tracked = self.tickets[0]
        self.assertEqual(self.client.get(base + "/tracks").status_code, 401)
        self.login()
        track = base + "/tracks/" + tracked
        self.assertEqual(self.client.post(base + "/tracks/" + self.tickets[-1]).status_code, 404)
        self.assertEqual(self.client.post(track).status_code, 201)
        self.assertEqual(self.client.post(track).status_code, 200)
        self.assertEqual(self.client.get(base + "/tracks").json()["items"][0]["ticket_uuid"], tracked)
        self.assertEqual(self.client.get(base + "/notifications").json()["count"], 0)
        writer = Client()
        self.cur.execute("""
            INSERT INTO staff_user(company_uuid,email,name,password_hash)
            SELECT company_uuid,'followup@qa1.test','메모 작성자',password_hash
            FROM staff_user WHERE staff_user_uuid=%s RETURNING staff_user_uuid
        """, (self.employees[0],))
        staff = self.cur.fetchone()[0]
        self.addCleanup(self.cur.execute, "DELETE FROM staff_user WHERE staff_user_uuid=%s", (staff,))
        self.addCleanup(self.cur.execute, "DELETE FROM ticket_message WHERE staff_user_uuid IN (%s,%s)",
                        (staff, self.employees[0]))
        self.cur.execute("""
            INSERT INTO staff_permission SELECT %s,permission_uuid FROM permission
            WHERE code IN ('ticket.read','ticket.handle')
        """, (staff,))
        logged_in = writer.post("/api/auth/login", {"email": "followup@qa1.test", "password": "SamplePass1!"},
                                content_type="application/json")
        self.assertEqual(logged_in.status_code, 200)
        self.cur.execute("""
            INSERT INTO staff_permission SELECT %s,permission_uuid FROM permission
            WHERE code='ticket.handle' ON CONFLICT DO NOTHING
        """, (self.employees[0],))
        self.addCleanup(self.cur.execute, """
            DELETE FROM staff_permission USING permission
            WHERE staff_permission.permission_uuid=permission.permission_uuid
              AND staff_user_uuid=%s AND code IN ('ticket.handle','chat.handle')
        """, (self.employees[0],))
        for client in (self.client, writer):
            self.assertEqual(client.post(f"/api/tickets/{tracked}/memos", {"body": "팔로업 메모"},
                                         content_type="application/json").status_code, 201)
        self.assertEqual(writer.post(f"/api/tickets/{self.tickets[-1]}/memos", {"body": "권한 밖 메모"},
                                     content_type="application/json").status_code, 404)
        event = str(uuid4())
        message = "followup-" + event
        self.cur.execute("""
            INSERT INTO voc.bronze_messages VALUES(%s,'tenant-1','gmail','account',%s,'qa',now(),'{}','{}')
        """, (event, message))
        self.cur.execute("""
            INSERT INTO voc.silver_messages(schema_version,event_id,company_id,channel,connection_id,
                source_message_id,source_conversation_id,message_kind,sender_ref,
                body_text,body_version,received_at,source_metadata)
            VALUES (4,%s,'tenant-1','gmail','account',%s,'thread','customer_inquiry','customer_10',
                    '배송 후속 문의',1,now(),'{"label_ids":[],"to_addresses":[]}')
        """, (event, message))
        self.cur.execute("SELECT ticket_uuid FROM ticket WHERE source_event_id=%s", (event,))
        followup = str(self.cur.fetchone()[0])
        self.addCleanup(self.cur.execute, "UPDATE voc.silver_messages SET message_kind='other' WHERE event_id=%s",
                        (event,))
        notifications = self.client.get(base + "/notifications").json()
        self.assertEqual(notifications["count"], 2)
        self.assertEqual(self.client.get(base + "/notifications", {"summary": "1"}).json(),
                         {"count": 2, "ticket_count": 1})
        state = self.client.get(reverse("followup:page-data")).json()
        self.assertEqual(state["tracks"][0]["ticket_uuid"], tracked)
        self.assertEqual({item["kind"] for item in state["notifications"]}, {"memo", "customer_inquiry"})
        self.assertEqual({item["kind"] for item in notifications["items"]}, {"memo", "customer_inquiry"})
        self.assertEqual(next(item for item in notifications["items"] if item["kind"] == "customer_inquiry")
                         ["related_ticket_uuid"], followup)
        self.cur.execute("UPDATE ticket SET status='답변완료' WHERE ticket_uuid=%s", (tracked,))
        self.addCleanup(self.cur.execute, "UPDATE ticket SET status='미처리' WHERE ticket_uuid=%s", (tracked,))
        self.assertEqual(self.client.get(base + "/notifications").json()["count"], 1)
        self.assertEqual(writer.get(base + "/notifications").json()["count"], 0)

        contacts = base + "/contacts"
        response = self.client.post(contacts, {
            "ticket_uuid": tracked, "assignee_uuid": self.employees[0],
            "reason": "배송 안내", "scheduled_at": "2026-11-25T14:00:00+09:00",
        }, content_type="application/json")
        self.assertEqual(response.status_code, 201, response.text)
        ident = response.json()["contact_uuid"]
        self.assertEqual(self.client.get(contacts, {"q": "배송 안내", "source": "ticket"}).json()["items"][0]
                         ["contact_uuid"], ident)
        self.assertEqual(writer.get(f"{contacts}/{ident}").status_code, 404)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.cookies.update(self.client.cookies)
        self.assertEqual(csrf_client.post(f"{contacts}/{ident}/complete").status_code, 403)
        done = {"result": "배송 완료 확인", "contacted_at": "2026-11-25T14:12:00+09:00"}
        for result, expected in ((done, 200), (done, 200), ({**done, "result": "다름"}, 409)):
            response = self.client.post(f"{contacts}/{ident}/complete", result, content_type="application/json")
            self.assertEqual(response.status_code, expected, response.text)
        self.assertEqual(self.client.get(contacts).json()["items"], [])
        result = self.client.get(contacts, {"state": "completed", "q": "배송 완료"}).json()["items"][0]
        self.assertEqual(result["completed_by_uuid"], self.employees[0])
        self.assertNotEqual(result["scheduled_at"], result["contacted_at"])
        self.assertEqual(self.client.get(result["source_url"]).status_code, 200)
        self.assertEqual(self.client.delete(track).status_code, 204)
        self.assertEqual(self.client.get(base + "/tracks").json()["items"], [])
        self.assertEqual(self.client.get(base + "/notifications").json()["count"], 0)

        self.cur.execute("""
            INSERT INTO chat(company_uuid,customer_uuid,domain_uuid,channel_uuid,status,waiting_at)
            SELECT company_uuid,customer_uuid,domain_uuid,channel_uuid,'waiting',now()
            FROM ticket WHERE ticket_uuid=%s RETURNING chat_uuid
        """, (tracked,))
        chat = str(self.cur.fetchone()[0])
        self.cur.execute("""
            INSERT INTO chat_message(chat_uuid,sender_type,message_type,body,sent_at)
            VALUES (%s,'customer','inquiry','배송지 변경 상담',now())
        """, (chat,))
        appointment = {"chat_uuid": chat, "assignee_uuid": self.employees[0],
                       "reason": "상담 후 연락", "scheduled_at": "2026-11-26T10:30:00+09:00"}
        self.assertEqual(self.client.post(contacts, appointment, content_type="application/json").status_code, 403)
        self.cur.execute("""
            INSERT INTO staff_permission SELECT %s,permission_uuid FROM permission WHERE code='chat.handle'
        """, (self.employees[0],))
        response = self.client.post(contacts, appointment, content_type="application/json")
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(self.client.get(contacts, {"source": "ticket"}).json()["items"], [])
        [consultation] = self.client.get(contacts, {"source": "chat", "q": "배송지 변경"}).json()["items"]
        self.assertEqual(self.client.get(consultation["source_url"]).json()["messages"][0]["body"], "배송지 변경 상담")
        self.assertEqual(writer.get(consultation["source_url"]).status_code, 403)
        chat_page = reverse("followup:chat-page", args=[chat])
        state = self.client.get(reverse("followup:page-data")).json()
        self.assertTrue(any(item["chat_uuid"] == chat for item in state["contacts"]))
        self.assertContainsHTML(self.client.get(chat_page), "배송지 변경 상담")
        self.assertEqual(writer.get(chat_page).status_code, 403)
        self.login(company=2)
        self.assertEqual(self.client.get(f"{contacts}/{ident}").status_code, 404)

    def test_followup_sidebar_reflects_a_dashboard_grant_in_the_current_session(self):
        self.login()
        url = reverse("followup:page")
        staff = self.employees[0]
        self.cur.execute("SELECT permission_uuid FROM permission WHERE code='analytics.read'")
        permission = self.cur.fetchone()[0]
        remove = "DELETE FROM staff_permission WHERE staff_user_uuid=%s AND permission_uuid=%s"
        self.addCleanup(self.cur.execute, remove, (staff, permission))

        before = self.client.get(url)
        self.assertEqual(before.status_code, 200)
        self.assertNotIn('href="/analytics/"', before.content.decode())
        self.cur.execute("INSERT INTO staff_permission(staff_user_uuid,permission_uuid) VALUES (%s,%s)", (staff, permission))
        self.assertContainsHTML(self.client.get(url), 'href="/analytics/"')
        self.cur.execute(remove, (staff, permission))
        revoked = self.client.get(url)
        self.assertEqual(revoked.status_code, 200)
        self.assertNotIn('href="/analytics/"', revoked.content.decode())

    def test_followup_page_and_api_mutations_use_django(self):
        page_url = reverse("followup:page")
        self.assertEqual(self.client.get(page_url).status_code, 302)
        self.login()
        self.assertContainsHTML(self.client.get(reverse("tickets:list")), f'href="{page_url}"')
        page = self.client.get(page_url)
        for value in ('id="follow-title"', 'id="follow-register"', "followup/page.css", "followup/page.js", "shared/ticats-logo.png"):
            self.assertContainsHTML(page, value)
        ticket = self.tickets[1]
        register = reverse("followup:track-schedule")
        state = reverse("followup:page-data")
        self.assertTrue(any(item["ticket_uuid"] == ticket for item in self.client.get(state).json()["tickets"]))
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.cookies.update(self.client.cookies)
        data = {"ticket_uuid": ticket, "scheduled_at": (timezone.now() + timedelta(hours=2)).isoformat()}
        headers = {"HTTP_X_CSRFTOKEN": csrf_client.cookies["csrftoken"].value}
        request_json = {"content_type": "application/json"}
        self.assertEqual(csrf_client.post(register, data, **request_json).status_code, 403)
        self.assertEqual(csrf_client.post(register, {**data, "scheduled_at": timezone.now().isoformat()},
                                          **request_json, **headers).status_code, 422)
        created = csrf_client.post(register, data, **request_json, **headers)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(csrf_client.post(register, data, **request_json, **headers).status_code, 409)
        snapshot = csrf_client.get(state).json()
        [contact] = [row for row in snapshot["contacts"] if row["contact_uuid"] == created.json()["contact_uuid"]]
        [tracked] = snapshot["tracks"]
        self.assertNotIn(ticket, {item["ticket_uuid"] for item in snapshot["tickets"]})
        self.assertEqual(contact["reason"], "추적 후속 연락")
        self.assertEqual(contact["assignee_uuid"], self.employees[0])
        self.assertTrue(contact["reminders_started_at"])
        self.assertIsNone(contact["reminders_stopped_at"])
        completed = csrf_client.post(contact["complete_url"],
                                     {"result": "고객 확인", "contacted_at": timezone.now().isoformat()},
                                     **request_json, **headers)
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertEqual(completed.json()["result"], "고객 확인")
        self.assertTrue(completed.json()["reminders_stopped_at"])
        self.assertEqual(csrf_client.delete(tracked["remove_url"], **headers).status_code, 204)
        self.assertEqual(csrf_client.get("/api/follow-up/tracks").json()["items"], [])
        self.assertTrue(any(item["result"] == "고객 확인" for item in csrf_client.get(state).json()["contacts"]))

    def test_followup_html_forms_render_and_persist_state(self):
        self.login()
        page = reverse("followup:page")
        ticket = self.tickets[0]
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.cookies.update(self.client.cookies)
        self.assertContainsHTML(csrf_client.get(page), f'value="{ticket}"')
        headers = {"HTTP_X_CSRFTOKEN": csrf_client.cookies["csrftoken"].value}
        register = {"action": "register", "ticket_uuid": ticket, "scheduled_at": "2000-01-01T12:00"}
        self.assertEqual(csrf_client.post(page, register).status_code, 403)
        invalid = csrf_client.post(page, register, **headers)
        self.assertEqual(invalid.status_code, 422)
        self.assertIn("현재보다 늦은 추적 날짜와 시각을 입력해 주세요.", invalid.text)
        register["scheduled_at"] = timezone.localtime(timezone.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
        tracked = csrf_client.post(page, register, follow=True, **headers)
        self.assertContainsHTML(tracked, "추적을 등록했습니다.")
        self.assertContainsHTML(tracked, f'href="{reverse("tickets:detail", args=[ticket])}"')
        self.assertContainsHTML(csrf_client.get(page, {"q": "no-matching-ticket"}), "조회 결과 0건")
        self.assertContainsHTML(csrf_client.get(page, {"tab": "scheduled"}), "추적 후속 연락")
        contacts = csrf_client.get(reverse("followup:page-data")).json()["contacts"]
        [contact] = [row for row in contacts if row["ticket_uuid"] == ticket and row["contacted_at"] is None]
        response = csrf_client.post(page + "?tab=scheduled&selected=" + contact["contact_uuid"], {
            "action": "complete", "contact_uuid": contact["contact_uuid"], "result": "Django 폼으로 연락 완료",
            "contacted_at": timezone.now().isoformat(),
        }, follow=True, **headers)
        self.assertContainsHTML(response, "연락 완료를 저장했습니다.")
        self.assertContainsHTML(response, "Django 폼으로 연락 완료")
        response = csrf_client.post(page, {"action": "untrack", "ticket_uuid": ticket}, follow=True, **headers)
        self.assertContainsHTML(response, "추적을 해제했습니다.")
        self.assertContainsHTML(response, "조회 결과 0건")

    def test_inbox_links_received_transfers_and_reviews_to_their_own_ticket(self):
        transfer_ticket, review_ticket = self.tickets[:2]
        recipient, outsider = self.employees
        self.cur.execute("""
            INSERT INTO staff_user(company_uuid,email,name,password_hash)
            SELECT company_uuid,'agent2@qa1.test','요청 상담사',password_hash FROM staff_user WHERE staff_user_uuid=%s
            RETURNING staff_user_uuid
        """, (recipient,))
        sender = self.cur.fetchone()[0]
        self.addCleanup(self.delete_inbox_fixture, sender)
        self.cur.execute("INSERT INTO staff_permission SELECT %s,permission_uuid FROM permission WHERE code='ticket.read'", (sender,))
        requests = {}
        for kind, ticket, receiver, memo in (
            ("transfer", transfer_ticket, recipient, "배송 담당 변경"),
            ("review", review_ticket, recipient, "응대 문구 검토"),
            ("outside", review_ticket, outsider, "다른 회사 요청"),
            ("other", review_ticket, sender, "다른 상담사 요청"),
        ):
            self.cur.execute("""
                INSERT INTO collab_request(ticket_uuid,requester_uuid,recipient_uuid,request_type,memo)
                VALUES (%s,%s,%s,%s,%s) RETURNING collab_request_uuid
            """, (ticket, sender, receiver, "transfer" if kind == "transfer" else "review", memo))
            requests[kind] = self.cur.fetchone()[0]
        self.cur.execute("""
            INSERT INTO collab_request_attachment(collab_request_uuid,original_filename,content_type,storage_key,size_bytes)
            VALUES (%s,'검토.txt','text/plain','private/fixture',12)
        """, (requests["review"],))
        self.login()
        for kind, ticket, memo in (("transfer", transfer_ticket, "배송 담당 변경"), ("review", review_ticket, "응대 문구 검토")):
            with self.subTest(kind=kind):
                page = self.client.get("/inbox/", {"tab": kind, "request": requests[kind]})
                self.assertContainsHTML(page, memo)
                detail = reverse("tickets:detail", args=[ticket])
                self.assertContainsHTML(page, f'href="{detail}">티켓 열기')
                self.assertEqual(self.client.get(detail).status_code, 200)
        self.assertContainsHTML(page, "검토.txt")
        self.assertEqual({tab: (count, attention) for tab, _, count, attention in page.context["tabs"]},
                         {"transfer": (1, 1), "review": (1, 1), "sent_transfer": (0, 0), "sent_review": (0, 0)})
        self.assertContainsHTML(page, "검토 대기")
        self.assertNotIn('class="inbox-unread-label"', page.text)
        self.assertNotIn("private/fixture", page.text)
        self.assertNotIn("다른 상담사 요청", page.text)
        self.assertEqual(self.client.get("/inbox/", {"request": requests["other"]}).status_code, 404)
        read = reverse("workspace:inbox-read", args=["request", requests["review"]])
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.cookies.update(self.client.cookies)
        self.assertEqual(csrf_client.post(read).status_code, 403)
        first = csrf_client.post(read, HTTP_X_CSRFTOKEN=csrf_client.cookies["csrftoken"].value)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.client.post(read).json()["read_at"], first.json()["read_at"])
        self.assertNotIn("응대 문구 검토", self.client.get("/inbox/", {"tab": "review", "scope": "unread"}).text)
        read_page = self.client.get("/inbox/", {"tab": "review"})
        self.assertContainsHTML(read_page, "검토 대기")
        self.assertEqual(read_page.context["tabs"][1][2:], (1, 1))
        reply = reverse("workspace:inbox-reply", args=[requests["review"]])
        self.assertEqual(self.client.post(reply, {"body": "확인했습니다."}).status_code, 200)
        self.assertEqual(self.client.post(reply, {"body": "확인했습니다."}).status_code, 200)
        self.assertEqual(self.client.post(reply, {"body": "다른 답변"}).status_code, 409)
        self.login(2)
        self.assertNotIn("다른 회사 요청", self.client.get("/inbox/", {"tab": "review"}).text)
        self.assertEqual(self.client.post(read).status_code, 404)
        self.client.post("/api/auth/login", {"email": "agent2@qa1.test", "password": "SamplePass1!"}, content_type="application/json")
        self.cur.execute("SELECT collab_review_reply_uuid FROM collab_review_reply WHERE collab_request_uuid=%s", (requests["review"],))
        reply_id = self.cur.fetchone()[0]
        sent_transfer = self.client.get("/inbox/", {"tab": "sent_transfer", "request": requests["transfer"]})
        self.assertContainsHTML(sent_transfer, "보낸 이관 요청 상세")
        self.assertContainsHTML(sent_transfer, "수락 대기")
        self.assertNotIn('id="request-accept"', sent_transfer.text)
        sent_review = self.client.get("/inbox/", {"tab": "sent_review", "request": requests["review"]})
        self.assertContainsHTML(sent_review, "보낸 검토 요청 상세")
        self.assertContainsHTML(sent_review, "새 답변")
        self.assertContainsHTML(sent_review, "확인했습니다.")
        self.assertNotIn("다른 회사 요청", sent_review.text)
        self.assertContainsHTML(self.client.get("/inbox/", {"tab": "reply", "request": reply_id}), "확인했습니다.")
        self.assertEqual(self.client.post(reverse("workspace:inbox-read", args=["reply", reply_id])).status_code, 200)
        unread = self.client.get("/inbox/", {"tab": "sent_review", "scope": "unread"})
        self.assertNotIn("확인했습니다.", unread.text)
        self.assertEqual(unread.context["tabs"][3][2:], (2, 0))
        self.assertContainsHTML(self.client.get("/inbox/", {"tab": "reply", "scope": "unread"}), "확인했습니다.")

    def delete_inbox_fixture(self, sender):
        for table in ("collab_review_reply", "collab_request_attachment"):
            self.cur.execute(sql.SQL("""
                DELETE FROM {} WHERE collab_request_uuid IN (
                    SELECT collab_request_uuid FROM collab_request WHERE requester_uuid=%s
                )
            """).format(sql.Identifier(table)), (sender,))
        self.cur.execute("DELETE FROM collab_request WHERE requester_uuid=%s", (sender,))
        self.cur.execute("DELETE FROM staff_user WHERE staff_user_uuid=%s", (sender,))

    def login(self, company=1):
        response = self.client.post("/api/auth/login", data={"email": f"agent1@qa{company}.test", "password": "SamplePass1!"}, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.cookies["voc_session"]["httponly"])

    def test_super_admin_with_ticket_work_permission_can_open_current_company_tickets(self):
        self.cur.execute("""
            INSERT INTO staff_user(company_uuid,email,name,password_hash,role)
            SELECT company_uuid,'operator@qa1.test','Operator',password_hash,'super_admin'
            FROM staff_user WHERE staff_user_uuid=%s RETURNING staff_user_uuid
        """, (self.employees[0],))
        operator = self.cur.fetchone()[0]
        self.addCleanup(self.cur.execute, "DELETE FROM staff_user WHERE staff_user_uuid=%s", (operator,))
        self.cur.execute("INSERT INTO staff_permission SELECT %s,permission_uuid FROM permission WHERE code='ticket.handle'",
                         (operator,))
        auth = self.client.post("/api/auth/login", {"email": "operator@qa1.test", "password": "SamplePass1!"},
                                content_type="application/json")
        self.assertEqual(auth.status_code, 200)
        response = self.client.get("/api/tickets", {"scope": "all"})
        self.assertEqual(response.status_code, 200)
        ticket = response.json()["items"][0]["ticket_uuid"]
        self.assertTrue(self.client.get(f"/tickets/{ticket}/").context["can_conduct"])
        self.cur.execute("DELETE FROM staff_permission WHERE staff_user_uuid=%s", (operator,))
        self.assertEqual(self.client.get("/api/tickets").status_code, 403)

    def test_identity_tenant_permissions_and_logout(self):
        self.assertEqual(self.client.get("/api/tickets").status_code, 401)
        self.login()
        self.assertEqual(self.client.get("/api/auth/me").json()["email"], "agent1@qa1.test")
        self.assertEqual(self.client.get(f"/api/tickets/{self.tickets[-1]}").status_code, 404)
        self.cur.execute("DELETE FROM staff_permission WHERE staff_user_uuid=%s", (self.employees[0],))
        try:
            for path in ("/api/tickets", "/api/tickets/options", f"/api/tickets/{self.tickets[0]}"):
                self.assertEqual(self.client.get(path).status_code, 403)
        finally:
            self.cur.execute("INSERT INTO staff_permission SELECT %s,permission_uuid FROM permission WHERE code='ticket.read'",
                             (self.employees[0],))
        issued_cookie = self.client.cookies["voc_session"].value
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 204)
        self.client.cookies.clear()
        self.client.cookies["voc_session"] = issued_cookie
        self.assertEqual(self.client.get("/api/tickets").status_code, 401)

    def test_stable_pages_and_total_after_last_page(self):
        self.login(company=2)
        first = self.client.get("/api/tickets", data={"page_size": 10}).json()
        last = self.client.get("/api/tickets", data={"page_size": 10, "page": 99}).json()
        self.assertEqual((first["total"], last["total"], last["page"]), (12, 12, 2))
        self.assertEqual(len(first["items"]), 10)
        self.assertEqual(len(last["items"]), 2)
        seen = [row["ticket_uuid"] for row in first["items"] + last["items"]]
        self.assertEqual(len(set(seen)), 12)
        desc = self.client.get("/api/tickets", data={"sort": "received-desc"}).json()
        self.assertEqual(desc["items"][0]["ticket_uuid"], seen[-1])

    def test_independent_filters_search_and_kst_calendar(self):
        self.login()
        def items(**params):
            response = self.client.get("/api/tickets", data=params)
            self.assertEqual(response.status_code, 200, response.text)
            return [row["ticket_uuid"] for row in response.json()["items"]]

        a, b, discord, *_ = self.tickets
        self.assertEqual(items(), [a, discord, b])
        self.assertEqual(items(team=self.team,assignee="unassigned"), [a])
        self.assertEqual(items(scope="mine",status="보류",sentiment="부정"), [b])
        self.assertEqual(items(start="2024-02-02", end="2024-02-02"), [a, discord])
        self.assertEqual(items(search_by="id",q="#00000002"), [b])
        self.assertEqual(items(search_by="customer",q="_10"), [a, discord, b])
        self.assertEqual(items(search_by="customer",q="%"), [])
        for params in ({"q":"가"*51}, {"days":2}, {"start":"2024-01-01","end":"2024-12-31"},
                       {"start":"2024-01-01"}, {"assignee":"bad"}, {"sort":"x"}):
            self.assertEqual(self.client.get("/api/tickets",data=params).status_code, 422)
        options = self.client.get("/api/tickets/options").json()
        self.assertEqual([item["id"] for item in options["assignees"]], [self.employees[0]])
        self.assertEqual(options["teams"], [{"id":self.team,"name":"배송팀"}])

    def test_customer_timeline_and_bidirectional_candidate_visibility(self):
        a,b,discord,*_ = self.tickets
        self.login()
        def detail(ident):
            response = self.client.get(f"/api/tickets/{ident}")
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()
        data = detail(a)
        self.assertEqual([m["ticket_uuid"] for m in data["messages"]], [a])
        self.assertNotIn("customer_email", data["ticket"])
        self.assertNotIn("staff_user_uuid", data["messages"][0])
        self.assertEqual([row["ticket_uuid"] for row in data["customer_tickets"]], [b])
        self.assertEqual([c["ticket_uuid"] for c in data["duplicate_candidates"]], [discord])
        self.assertEqual([c["ticket_uuid"] for c in detail(discord)["duplicate_candidates"]], [a])
        self.assertEqual(detail(b)["duplicate_candidates"], [])
        with self.assertRaises(psycopg2.Error):
            self.cur.execute("INSERT INTO ticket_duplicate_candidate VALUES (%s,%s)", sorted((a,self.tickets[-1])))
        self.assertEqual([row["ticket_uuid"] for row in detail(a)["duplicate_candidates"]], [discord])
        self.assertEqual(data["ticket"]["sentiment_status"], "pending")
        self.assertIsNone(data["ticket"]["sentiment"])
        self.assertEqual(self.client.get(f"/api/tickets/{self.tickets[-1]}").status_code, 404)
        self.cur.execute("UPDATE voc.silver_messages SET message_kind='unknown' WHERE event_id="
                         "(SELECT source_event_id FROM ticket WHERE ticket_uuid=%s)", (discord,))
        try:
            self.assertEqual(detail(a)["duplicate_candidates"], [])
        finally:
            self.cur.execute("UPDATE voc.silver_messages SET message_kind='customer_inquiry' WHERE event_id="
                             "(SELECT source_event_id FROM ticket WHERE ticket_uuid=%s)", (discord,))


    def test_desk_html_and_csrf(self):
        browser = Client(enforce_csrf_checks=True)
        self.assertEqual(browser.get("/tickets/?scope=mine").status_code, 302)
        self.assertEqual(browser.get("/login/").status_code, 200)
        credentials = {"email": "agent1@qa1.test", "password": "SamplePass1!"}
        self.assertEqual(browser.post("/login/", credentials).status_code, 403)
        credentials["csrfmiddlewaretoken"] = browser.cookies["csrftoken"].value
        credentials["next"] = "/tickets/?scope=mine"
        response = browser.post("/login/", credentials)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/tickets/?scope=mine")
        self.assertEqual(response.cookies["voc_desk_session"]["path"], "/")
        self.assertTrue(response.cookies["voc_desk_session"]["httponly"])
        listing = browser.get(response["Location"])
        self.assertContainsHTML(listing, "배송 요청 2")
        detail_path = f"/tickets/{self.tickets[1]}/"
        self.assertContainsHTML(listing, detail_path + "?scope=mine")
        detail = browser.get(detail_path + "?scope=mine")
        self.assertContainsHTML(detail, "배송 요청 2")
        self.assertContainsHTML(detail, 'class="detail-meta"')
        self.assertEqual(detail.context["conversation"][0]["body"], "배송 요청 2")
        self.grant_conduct()
        detail_url = f"/tickets/{self.tickets[1]}/"
        memo = {"message_type": "memo", "body": "브라우저 메모", "request_key": str(uuid4())}
        self.addCleanup(self.cur.execute, "DELETE FROM ticket_message WHERE request_key=%s", (memo["request_key"],))
        self.assertEqual(browser.post(detail_url + "messages/", memo).status_code, 403)
        memo["csrfmiddlewaretoken"] = browser.cookies["csrftoken"].value
        saved = browser.post(detail_url + "messages/?scope=mine", memo, follow=True)
        self.assertContainsHTML(saved, "브라우저 메모")
        self.assertEqual(saved.redirect_chain, [(detail_url + "?scope=mine&memos=1", 302)])
        self.assertContainsHTML(saved, 'id="detail-memos" open')
        self.assertEqual(browser.get(f"/tickets/{self.tickets[-1]}/").status_code, 404)
        self.assertEqual(browser.post("/logout/").status_code, 403)
        self.assertEqual(browser.post("/logout/", {"csrfmiddlewaretoken":browser.cookies["csrftoken"].value}).status_code, 302)
        self.assertEqual(browser.get("/tickets/").status_code, 302)

    def test_existing_api_session_can_open_html_without_new_login(self):
        self.login()
        token = self.client.cookies["voc_session"].value
        self.client.cookies.clear()
        redirect_response = self.client.get("/tickets/?scope=mine")
        self.assertEqual(redirect_response.status_code, 302)
        self.assertTrue(redirect_response.url.startswith("/api/auth/resume?"))
        # The previous browser cookie was scoped to /api, so it first arrives here.
        self.client.cookies["voc_session"] = token
        resumed = self.client.get(redirect_response.url)
        self.assertEqual(resumed.url, "/tickets/?scope=mine")
        self.assertEqual(resumed.cookies["voc_desk_session"].value, token)
        self.assertEqual(self.client.get(resumed.url).status_code, 200)

    def test_list_link_opens_detail_and_memo_requires_conduct_permission(self):
        self.login()
        ticket = self.tickets[0]
        path = f"/tickets/{ticket}/"
        listing = self.client.get("/tickets/?scope=all")
        href = re.search(rf'href="({path}[^"]*)"', listing.text).group(1)
        self.assertContainsHTML(self.client.get(href), "배송 요청 1")
        payload = {"message_type": "internal_memo", "body": "내부 확인 🙂", "request_key": str(uuid4())}
        endpoint = f"/api/tickets/{ticket}/messages"
        self.assertEqual(self.client.post(endpoint, payload, content_type="application/json").status_code, 403)
        self.grant_conduct()
        detail = self.client.get(href)
        self.assertContainsHTML(detail, "내부 메모 작성")
        self.assertNotIn("고객에게 답변", detail.text)
        self.assertNotIn("분석·배정 수정", detail.text)
        with patch("apps.tickets.delivery.send_reply") as send:
            denied = self.client.post(endpoint, {**payload, "message_type": "reply"},
                                      content_type="application/json")
            self.assertEqual(denied.status_code, 403)
            send.assert_not_called()
        first = self.client.post(endpoint, payload, content_type="application/json")
        again = self.client.post(endpoint, payload, content_type="application/json")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), again.json())
        self.assertEqual(first.json()["ticket_status"], "미처리")
        self.assertContainsHTML(self.client.get(path), "내부 확인 🙂")
        data = self.client.get(f"/api/tickets/{ticket}").json()
        self.assertEqual([memo["body"] for memo in data["memos"]], ["내부 확인 🙂"])
        self.assertNotIn("내부 확인 🙂", [message["body"] for message in data["messages"]])
        self.assertEqual(self.client.get(f"/api/tickets/{self.tickets[1]}").json()["memos"], [])
        self.cur.execute("DELETE FROM staff_permission WHERE staff_user_uuid=%s AND permission_uuid "
                         "IN (SELECT permission_uuid FROM permission WHERE code='ticket.handle')", (self.employees[0],))
        self.assertNotIn("내부 확인 🙂", self.client.get(path).text)
        self.assertEqual(self.client.get(f"/api/tickets/{ticket}").json()["memos"], [])
        self.assertEqual(self.client.get("/api/tickets").json()["total"], 3)
        self.cur.execute("DELETE FROM ticket_message WHERE request_key=%s", (payload["request_key"],))

    def test_reply_uses_collected_discord_route_and_completes_only_sent_ticket(self):
        self.login()
        self.grant_conduct()
        first, _, discord, *_ = self.tickets
        self.assign_unassigned_ticket(discord)
        endpoint = f"/api/tickets/{discord}/messages"
        payload = {"message_type": "reply", "body": "회신입니다", "request_key": str(uuid4())}
        with patch("apps.tickets.delivery.send_reply", return_value="outbound-123") as send:
            sent = self.client.post(endpoint, payload, content_type="application/json")
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertEqual(sent.json()["ticket_status"], "답변완료")
        self.assertEqual(send.call_args.args[0]["source_channel_id"], "101")
        self.assertEqual(send.call_args.args[0]["source_message_id"], "1")
        untouched = self.client.get(f"/api/tickets/{first}").json()
        self.assertEqual(untouched["ticket"]["status"], "미처리")
        self.cur.execute("DELETE FROM ticket_message WHERE ticket_uuid=%s AND message_type='reply'", (discord,))
        self.cur.execute("UPDATE ticket SET status='미처리' WHERE ticket_uuid=%s", (discord,))

    def test_assignee_can_mark_ticket_complete_without_sending_a_reply(self):
        self.login()
        self.grant_conduct()
        ticket = self.tickets[0]
        path = reverse("tickets:detail", args=[ticket])
        complete = reverse("tickets:complete", args=[ticket])
        self.assertNotIn("처리 완료</button>", self.client.get(path).text)
        self.assertEqual(self.client.post(complete).status_code, 403)
        self.assign_unassigned_ticket(ticket)
        self.addCleanup(self.cur.execute, "UPDATE ticket SET status='미처리' WHERE ticket_uuid=%s", (ticket,))
        self.addCleanup(self.cur.execute, """
            DELETE FROM ticket_event USING event_type WHERE ticket_event.event_type_uuid=event_type.event_type_uuid
              AND ticket_uuid=%s AND code='ticket_manually_completed'
        """, (ticket,))
        self.assertContainsHTML(self.client.get(path), "처리 완료</button>")
        self.assertEqual(self.client.get(complete).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.cookies.update(self.client.cookies)
        self.assertEqual(csrf_client.post(complete).status_code, 403)

        with patch("apps.tickets.delivery.send_reply") as send:
            first = self.client.post(complete + "?scope=mine")
            self.assertEqual((first.status_code, first.url), (302, path + "?scope=mine"))
            self.assertEqual(self.client.post(complete).status_code, 302)
            send.assert_not_called()
        detail = self.client.get(path)
        self.assertContainsHTML(detail, 'data-ticket-status="답변완료"')
        self.assertNotIn("처리 완료</button>", detail.text)
        self.cur.execute("""
            SELECT code,actor_uuid::text,from_status,to_status FROM ticket_event JOIN event_type USING(event_type_uuid)
            WHERE ticket_uuid=%s AND code='ticket_manually_completed'
        """, (ticket,))
        self.assertEqual(self.cur.fetchall(), [("ticket_manually_completed", self.employees[0], "미처리", "답변완료")])
        self.cur.execute("SELECT count(*) FROM ticket_message WHERE ticket_uuid=%s AND message_type='reply'", (ticket,))
        self.assertEqual(self.cur.fetchone()[0], 0)

    def test_gmail_failure_preserves_status_and_uses_original_headers(self):
        from apps.tickets.delivery import DeliveryError

        self.login()
        self.grant_conduct()
        ticket = self.tickets[0]
        self.assign_unassigned_ticket(ticket)
        self.cur.execute("SELECT source_event_id FROM ticket WHERE ticket_uuid=%s", (ticket,))
        source = self.cur.fetchone()[0]
        raw = {"payload": {"headers": [{"name": "Message-ID", "value": " <original@example.com> "},
                                      {"name": "Subject", "value": "원래\r\n 제목"}]}}
        self.cur.execute("UPDATE voc.bronze_messages SET raw_payload=%s WHERE event_id=%s", (json.dumps(raw), source))
        self.cur.execute("UPDATE voc.silver_messages SET title='원래 제목' WHERE event_id=%s", (source,))
        self.addCleanup(self.cur.execute, "UPDATE voc.bronze_messages SET raw_payload='{}' WHERE event_id=%s", (source,))
        self.addCleanup(self.cur.execute, "UPDATE voc.silver_messages SET title=NULL WHERE event_id=%s", (source,))
        key = str(uuid4())
        self.addCleanup(self.cur.execute, "DELETE FROM ticket_message WHERE request_key=%s", (key,))
        with patch("apps.tickets.delivery.send_reply", side_effect=DeliveryError("failed")) as send:
            result = self.client.post(f"/api/tickets/{ticket}/messages",
                                      {"message_type": "reply", "body": "답변", "request_key": key},
                                      content_type="application/json")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["message"]["delivery_status"], "failed")
        self.assertEqual(result.json()["ticket_status"], "미처리")
        route = send.call_args.args[0]
        self.assertEqual((route["source_conversation_id"], route["rfc_message_id"], route["original_subject"]),
                         ("thread", "<original@example.com>", "원래 제목"))

    def test_explicit_label_becomes_gold_on_first_successful_reply(self):
        self.login()
        self.grant_conduct()
        source = str(uuid4())
        self.cur.execute("SELECT clock_timestamp()")
        arrival = self.cur.fetchone()[0]
        self.cur.execute("""
            INSERT INTO voc.bronze_messages VALUES (%s,'tenant-1','gmail','account',%s,'test',%s,'{}','{}')
        """, (source, source, arrival))
        self.cur.execute("""
            INSERT INTO voc.silver_messages(schema_version,event_id,company_id,channel,connection_id,
                source_message_id,source_conversation_id,message_kind,sender_ref,body_text,
                body_version,received_at,source_metadata)
            VALUES (4,%s,'tenant-1','gmail','account',%s,'gold-thread','customer_inquiry','gold-customer',
                '정제 입력입니다',1,%s,'{"label_ids":[],"to_addresses":[]}')
        """, (source, source, arrival))
        self.cur.execute("SELECT ticket_uuid,feature_uuid FROM voc.feature_snapshot WHERE silver_event_id=%s", (source,))
        ticket, feature = self.cur.fetchone()
        self.addCleanup(self.cur.execute, "UPDATE voc.silver_messages SET message_kind='other' WHERE event_id=%s",
                        (source,))
        self.addCleanup(self.cur.execute, "DELETE FROM ticket_message WHERE ticket_uuid=%s AND message_type='reply'",
                        (ticket,))
        self.addCleanup(self.cur.execute, "DELETE FROM voc.gold_label WHERE feature_uuid=%s", (feature,))
        self.assign_unassigned_ticket(ticket)
        changed = self.client.post(reverse("assignment:ticket-category", args=[ticket]),
                                   {"sentiment": "매우 부정", "feature_uuid": str(feature)})
        self.assertEqual(changed.status_code, 302, changed.text)
        review = self.client.post(reverse("assignment:ticket-category", args=[ticket]),
                                  {"is_urgent": "yes", "reason": "배송 확인 필요", "feature_uuid": str(feature)})
        self.assertEqual(review.status_code, 302, review.text)
        self.cur.execute("""
            SELECT r.is_urgent,r.sentiment,e.transfer_reason
            FROM voc.counselor_review r JOIN ticket_event e ON e.ticket_event_uuid::text=r.evidence_ref
            WHERE r.feature_uuid=%s
        """, (feature,))
        self.assertEqual(self.cur.fetchall(), [(True, None, "배송 확인 필요")])
        path = f"/api/tickets/{ticket}/messages"
        payload = {"message_type": "reply", "body": "고객 답변", "request_key": str(uuid4())}
        with patch("apps.tickets.delivery.send_reply", return_value="delivery-1") as send:
            self.assertEqual(self.client.post(path, payload, content_type="application/json").status_code, 200)
            self.assertEqual(self.client.post(path, payload, content_type="application/json").status_code, 200)
        send.assert_called_once()
        self.cur.execute("""
            SELECT g.label_kind,g.sentiment_value,m.sent_at IS NOT NULL,g.evidence ? 'ticket_event_uuid'
            FROM voc.gold_label g JOIN public.ticket_message m ON m.ticket_message_uuid=g.completion_message_uuid
            WHERE g.feature_uuid=%s
        """, (feature,))
        self.assertEqual(self.cur.fetchall(), [("sentiment", "부정", True, True)])
        self.cur.execute("SELECT sentiment FROM ticket WHERE ticket_uuid=%s", (ticket,))
        self.assertEqual(self.cur.fetchone()[0], "매우 부정")

    def test_imported_csv_keeps_reply_internal_and_preserves_status(self):
        self.login()
        self.grant_conduct()
        self.cur.execute("""
            INSERT INTO ticket(company_uuid,domain_uuid,channel_uuid,customer_uuid,title,
                               created_at,updated_at,csv_import_kind)
            SELECT company_uuid,domain_uuid,channel_uuid,customer_uuid,'기존 상담 이력',
                   now(),now(),'migration' FROM ticket WHERE ticket_uuid=%s RETURNING ticket_uuid,ticket_number,company_uuid
        """, (self.tickets[0],))
        ticket, number, company = self.cur.fetchone()
        self.cur.execute("SELECT last_number FROM voc.ticket_number_counter WHERE company_uuid=%s", (company,))
        self.assertEqual(number, f'{self.cur.fetchone()[0]:08}')
        self.addCleanup(self.cur.execute, "DELETE FROM ticket WHERE ticket_uuid=%s", (ticket,))
        self.addCleanup(self.cur.execute, "DELETE FROM ticket_message WHERE ticket_uuid=%s", (ticket,))
        self.assign_unassigned_ticket(ticket)
        key = str(uuid4())
        with patch("apps.tickets.delivery.send_reply") as send:
            result = self.client.post(f"/api/tickets/{ticket}/messages",
                                      {"message_type": "reply", "body": "내부 처리 결과", "request_key": key},
                                      content_type="application/json")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["message"]["delivery_status"], "internal_only")
        self.assertEqual(result.json()["ticket_status"], "미처리")
        send.assert_not_called()

    def test_transfer_and_review_reach_only_the_recipient(self):
        self.login()
        self.grant_conduct()
        ticket, recipient = self.tickets[0], str(uuid4())
        self.cur.execute("SELECT company_uuid,password_hash FROM staff_user WHERE staff_user_uuid=%s",
                         (self.employees[0],))
        company, password_hash = self.cur.fetchone()
        self.cur.execute("INSERT INTO team(company_uuid,name) VALUES (%s,'이관 검증팀') RETURNING team_uuid",
                         (company,))
        recipient_team = str(self.cur.fetchone()[0])
        self.addCleanup(self.cur.execute, "DELETE FROM team WHERE team_uuid=%s", (recipient_team,))
        self.cur.execute("""INSERT INTO staff_user(staff_user_uuid,company_uuid,email,password_hash,team_uuid)
                         VALUES (%s,%s,'receiver@qa1.test',%s,%s)""", (recipient, company, password_hash, self.team))
        self.cur.execute("INSERT INTO staff_permission SELECT %s,permission_uuid FROM permission WHERE code IN "
                         "('ticket.read','ticket.handle')", (recipient,))
        self.cur.execute("INSERT INTO ticket_review_channel (company_uuid,name) VALUES (%s,'배송 검토') "
                         "RETURNING review_channel_uuid", (company,))
        channel = str(self.cur.fetchone()[0])
        self.cur.execute("INSERT INTO ticket_review_channel_member VALUES (%s,%s,%s)", (company, channel, recipient))
        self.addCleanup(self.cur.execute, "DELETE FROM staff_user WHERE staff_user_uuid=%s", (recipient,))
        self.addCleanup(self.cur.execute, "UPDATE ticket SET assignee_uuid=NULL,assigned_team_uuid=%s WHERE ticket_uuid=%s",
                        (self.team, ticket))
        self.addCleanup(self.cur.execute, "DELETE FROM ticket_review_channel WHERE review_channel_uuid=%s", (channel,))
        self.addCleanup(self.cur.execute, "DELETE FROM collab_request WHERE ticket_uuid=%s AND recipient_uuid=%s",
                        (ticket, recipient))
        self.addCleanup(self.cur.execute, "DELETE FROM collab_request_attachment WHERE collab_request_uuid IN "
                        "(SELECT collab_request_uuid FROM collab_request WHERE ticket_uuid=%s AND recipient_uuid=%s)",
                        (ticket, recipient))
        self.addCleanup(self.cur.execute, "DELETE FROM ticket_event WHERE ticket_uuid=%s AND recipient_uuid=%s",
                        (ticket, recipient))
        self.cur.execute("UPDATE ticket SET assignee_uuid=%s WHERE ticket_uuid=%s", (self.employees[0], ticket))

        options = self.client.get(f"/api/tickets/{ticket}/collaboration-options").json()
        self.assertEqual(options["transfer_recipients"][0]["staff_user_uuid"], recipient)
        transfer = self.client.post(f"/api/tickets/{ticket}/transfer",
                                    {"recipient_uuid": recipient, "reason": "배송 재확인", "attached_reply": "초안"},
                                    content_type="application/json")
        self.assertEqual(transfer.status_code, 201, transfer.text)
        self.assertEqual(transfer.json()["status"], "pending")
        transfer_request = transfer.json()["transfer_request_uuid"]
        accept_url = reverse("tickets:accept-transfer", args=[ticket, transfer_request])
        self.assertEqual(self.client.post(accept_url).status_code, 404)
        duplicate = self.client.post(f"/api/tickets/{ticket}/transfer",
                                     {"recipient_uuid": recipient, "reason": "한 번 더 요청"},
                                     content_type="application/json")
        self.assertEqual(duplicate.status_code, 409)
        review = self.client.post(f"/api/tickets/{ticket}/review-requests",
                                  {"review_channel_uuid": channel, "memo": "검토해 주세요",
                                   "attachment": {"content_type": "text/plain", "content": "확인 자료"}},
                                  content_type="application/json")
        self.assertEqual(review.status_code, 201, review.text)
        image = "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
        image_review = self.client.post(f"/api/tickets/{ticket}/review-requests",
                                        {"review_channel_uuid": channel, "memo": "이미지 검토",
                                         "attachment": {"content_type": "image/gif", "content": image}},
                                        content_type="application/json")
        self.assertEqual(image_review.status_code, 201, image_review.text)
        attachment_url = f"/api/ticket-requests/{image_review.json()['review_request_uuid']}/attachment"
        self.assertEqual(self.client.get("/api/ticket-requests").json()["items"], [])
        self.assertEqual(self.client.get(attachment_url).status_code, 404)
        receiver = Client()
        login = receiver.post("/api/auth/login", {"email": "receiver@qa1.test", "password": "SamplePass1!"},
                              content_type="application/json")
        self.assertEqual(login.status_code, 200, login.text)
        received = receiver.get("/api/ticket-requests").json()["items"]
        self.assertEqual({item["kind"] for item in received}, {"transfer", "review"})
        self.assertIn(["reference.gif"], [item["attachment_names"] for item in received])
        download = receiver.get(attachment_url)
        self.assertEqual(download.content, b64decode(image))
        self.assertEqual(download.headers["Content-Type"], "image/gif")
        self.assertEqual(receiver.get(f"/api/ticket-requests/{review.json()['review_request_uuid']}/attachment")
                         .content.decode("utf-8"), "확인 자료")
        self.assertContainsHTML(receiver.get("/inbox/", {"request": image_review.json()["review_request_uuid"]}),
                                attachment_url)
        [transfer_item] = [item for item in received if item["kind"] == "transfer"]
        self.assertEqual((transfer_item["id"], transfer_item["request_status"]), (transfer_request, "pending"))
        drawer = receiver.get("/inbox/", {"request": transfer_request})
        self.assertContainsHTML(drawer, f"/tickets/{ticket}/")
        self.assertContainsHTML(drawer, 'id="request-accept"')
        self.assertContainsHTML(drawer, "수락 대기")
        detail_url = f"/tickets/{ticket}/"
        category_url = detail_url + "category/"
        self.assertContainsHTML(self.client.get(detail_url), "분석·배정 수정")
        self.assertNotIn("분석·배정 수정", receiver.get(detail_url).text)
        self.assertEqual(receiver.get(category_url).status_code, 403)
        reply_payload = {"message_type": "reply", "body": "담당 상담사 답변", "request_key": str(uuid4())}
        with patch("apps.tickets.delivery.send_reply") as send:
            self.assertEqual(receiver.post(detail_url + "messages/", reply_payload).status_code, 403)
            send.assert_not_called()
        self.assertNotIn(ticket, [item["ticket_uuid"] for item in receiver.get("/api/tickets?scope=mine").json()["items"]])
        self.assertIn(ticket, [item["ticket_uuid"] for item in self.client.get("/api/tickets?scope=mine").json()["items"]])
        self.assertEqual(receiver.post(f"/api/inbox/request/{transfer_request}/read/").status_code, 200)
        self.cur.execute("SELECT status,decided_at FROM collab_request WHERE collab_request_uuid=%s", (transfer_request,))
        self.assertEqual(self.cur.fetchone(), ("pending", None))

        self.cur.execute("UPDATE ticket SET assignee_uuid=NULL WHERE ticket_uuid=%s", (ticket,))
        self.assertEqual(receiver.post(accept_url).status_code, 409)
        self.cur.execute("UPDATE ticket SET assignee_uuid=%s WHERE ticket_uuid=%s", (self.employees[0], ticket))
        self.cur.execute("UPDATE staff_user SET team_uuid=%s WHERE staff_user_uuid=%s", (recipient_team, recipient))
        for _ in range(2):
            self.assertEqual(receiver.post(accept_url).status_code, 200)

        self.assertIn(ticket, [item["ticket_uuid"] for item in receiver.get("/api/tickets?scope=mine").json()["items"]])
        self.assertNotIn(ticket, [item["ticket_uuid"] for item in self.client.get("/api/tickets?scope=mine").json()["items"]])
        self.assertContainsHTML(receiver.get(detail_url), "고객에게 답변")
        self.assertContainsHTML(receiver.get(detail_url), "분석·배정 수정")
        self.assertEqual(receiver.get(category_url).status_code, 200)
        self.assertEqual(self.client.post(category_url, {"category_uuid": str(uuid4())}).status_code, 403)
        with patch("apps.tickets.delivery.send_reply") as send:
            self.assertEqual(self.client.post(detail_url + "messages/", reply_payload).status_code, 403)
            send.assert_not_called()
        self.cur.execute("SELECT assigned_team_uuid::text FROM ticket WHERE ticket_uuid=%s", (ticket,))
        self.assertEqual(self.cur.fetchone()[0], recipient_team)
        self.cur.execute("SELECT count(*) FROM ticket_event WHERE collab_request_uuid=%s", (transfer_request,))
        self.assertEqual(self.cur.fetchone()[0], 1)
        accepted_drawer = receiver.get("/inbox/", {"request": transfer_request})
        self.assertContainsHTML(accepted_drawer, "수락 완료")
        self.assertEqual([(row["status_label"], row["needs_action"]) for row in accepted_drawer.context["rows"]],
                         [("수락 완료", False)])
        self.assertEqual(accepted_drawer.context["tabs"][0][2:], (1, 0))
        sent_drawer = self.client.get("/inbox/", {"tab": "sent_transfer", "request": transfer_request})
        self.assertContainsHTML(sent_drawer, "수락 완료")
        self.assertNotIn('id="request-accept"', accepted_drawer.text)

    def test_recommendations_follow_saved_category(self):
        self.login()
        self.grant_conduct()
        ticket = self.tickets[0]
        self.cur.execute("SELECT company_uuid,domain_uuid FROM ticket WHERE ticket_uuid=%s", (ticket,))
        company, domain = self.cur.fetchone()
        categories = []
        for name in ("제품 불량", "배송 지연"):
            self.cur.execute("INSERT INTO category(domain_uuid,name) VALUES (%s,%s) RETURNING category_uuid", (domain, name))
            category = self.cur.fetchone()[0]
            categories.append(category)
            self.cur.execute("""INSERT INTO quick_reply(company_uuid,domain_uuid,category_uuid,owner_uuid,name,body)
                             VALUES (%s,%s,%s,%s,%s,'답변 본문')""", (company, domain, category, self.employees[0], name))
        self.addCleanup(self.cur.execute, "DELETE FROM category WHERE category_uuid IN (%s,%s)", tuple(categories))
        self.addCleanup(self.cur.execute, "DELETE FROM quick_reply WHERE category_uuid IN (%s,%s)", tuple(categories))
        self.addCleanup(self.cur.execute, "UPDATE ticket SET category_uuid=NULL,category_status='pending' WHERE ticket_uuid=%s", (ticket,))
        path = f"/api/tickets/{ticket}/quick-replies"
        for category, expected in zip(categories, ("제품 불량", "배송 지연")):
            self.cur.execute("UPDATE ticket SET category_uuid=%s,category_status='succeeded' WHERE ticket_uuid=%s",
                             (category, ticket))
            result = self.client.get(path)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual([item["name"] for item in result.json()["items"]], [expected])

    def assign_unassigned_ticket(self, ticket):
        self.cur.execute("UPDATE ticket SET assignee_uuid=%s WHERE ticket_uuid=%s", (self.employees[0], ticket))
        self.addCleanup(self.cur.execute, "UPDATE ticket SET assignee_uuid=NULL WHERE ticket_uuid=%s", (ticket,))

    def grant_conduct(self):
        self.cur.execute("INSERT INTO staff_permission SELECT %s,permission_uuid FROM permission WHERE code='ticket.handle' "
                         "ON CONFLICT DO NOTHING", (self.employees[0],))
        self.addCleanup(self.cur.execute, "DELETE FROM staff_permission WHERE staff_user_uuid=%s AND permission_uuid "
                        "IN (SELECT permission_uuid FROM permission WHERE code='ticket.handle')", (self.employees[0],))

    def assertContainsHTML(self, response, content):
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(content, response.text)
