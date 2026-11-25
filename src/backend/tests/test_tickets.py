"""Test the API against the real PostgreSQL schema in a newly created disposable database."""
import os
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import psycopg2
from argon2 import PasswordHasher
from django.test import Client
from django.urls import reverse

from tests.database import use_database
from psycopg2 import sql
from psycopg2.extensions import make_dsn

SRC = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get("VOC_TICKET_TEST_ADMIN_DSN"), "Set VOC_TICKET_TEST_ADMIN_DSN for PostgreSQL tests")
class TicketDatabase(unittest.TestCase):
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
        for filename in ("schema.sql", "ticket_schema.sql", "settings_schema.sql", "followup_schema.sql", "monitoring.sql"):
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
        cls.cur.execute("UPDATE ticket SET classification_final_at=clock_timestamp(),sentiment_status='failed',category_status='failed'")
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

    def login(self, company=1, employee=1):
        response = self.client.post(
            "/api/auth/login", data={"email": f"agent{employee}@qa{company}.test", "password": "SamplePass1!"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.cookies["voc_session"]["httponly"])


class TicketTests(TicketDatabase):
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
        self.assertEqual(self.client.get(base + "/notifications").json()["count"], 1)
        self.cur.execute("""
            UPDATE ticket SET classification_final_at=clock_timestamp(),
                sentiment_status='succeeded',category_status='succeeded' WHERE ticket_uuid=%s
        """, (followup,))
        notifications = self.client.get(base + "/notifications").json()
        self.assertEqual(notifications["count"], 2)
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
        self.login(company=2)
        self.assertEqual(self.client.get(f"{contacts}/{ident}").status_code, 404)

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
        self.assertEqual(self.client.post(reverse("workspace:inbox-read", args=["reply", reply_id])).status_code, 200)
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

    def test_reply_adapter_confirms_once_with_the_same_idempotency_key(self):
        import json
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        from unittest.mock import patch

        attempts = []
        confirmed = set()

        class Adapter(BaseHTTPRequestHandler):
            def do_POST(self):
                key = self.headers["Idempotency-Key"]
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                attempts.append((key, payload))
                if key not in confirmed:
                    # The provider has accepted the first request, but the caller receives no proof.
                    confirmed.add(key)
                    self.send_error(503)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"delivery_confirmed":true,"provider_delivery_id":"local-confirmation"}')

            def log_message(self, *_):
                pass

        self.login(company=2)
        ticket, agent = self.tickets[-1], self.employees[-1]
        detail_url = reverse("tickets:detail", args=[ticket])
        classification_url = reverse("tickets:classification", args=[ticket])
        permissions_url = reverse("accounts:employee-permissions", args=[agent])
        reply_id = str(uuid4())
        url = f"/tickets/{ticket}/reply/"
        payload = {"reply_id": reply_id, "body": "확인했습니다."}
        self.cur.execute("UPDATE staff_user SET role='admin' WHERE staff_user_uuid=%s", (agent,))
        self.addCleanup(self.cur.execute, "UPDATE staff_user SET role=NULL WHERE staff_user_uuid=%s", (agent,))
        with patch.dict(os.environ, {"VOC_REPLY_URL": ""}):
            self.assertNotIn("분류 수정", self.client.get(detail_url).text)
            self.assertEqual(self.client.post(classification_url, {"sentiment": "중립"}).status_code, 403)
            self.assertEqual(self.client.post(url, payload).status_code, 403)
            self.assertEqual(self.client.post(permissions_url, {"permissions": ["ticket.handle"]}).status_code, 302)
            self.assertContainsHTML(self.client.get(detail_url), "회신 API 미연결")
            self.assertEqual(self.client.post(classification_url, {"sentiment": "중립"}).status_code, 302)
            self.assertEqual(self.client.post(url, payload).status_code, 503)
        with ThreadingHTTPServer(("127.0.0.1", 0), Adapter) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.dict(os.environ, {"VOC_REPLY_URL": f"http://127.0.0.1:{server.server_port}/reply"}):
                    self.assertContainsHTML(self.client.get(detail_url), "답변 보내기")
                    self.assertEqual(self.client.post(url, payload).status_code, 502)
                    self.cur.execute("SELECT count(*) FROM voc.ticket_answer_completion WHERE ticket_uuid=%s", (ticket,))
                    self.assertEqual(self.cur.fetchone()[0], 0)
                    self.assertContainsHTML(self.client.get(detail_url), "발송 다시 확인")
                    self.assertEqual(self.client.post(permissions_url, {}).status_code, 302)
                    self.assertNotIn("발송 다시 확인", self.client.get(detail_url).text)
                    self.assertEqual(self.client.post(url, payload).status_code, 403)
                    self.assertEqual(self.client.post(permissions_url, {"permissions": ["ticket.handle"]}).status_code, 302)
                    self.assertEqual(self.client.post(url, payload).status_code, 302)
                    self.assertEqual(self.client.post(url, payload).status_code, 302)
                    self.assertEqual(self.client.post(permissions_url, {}).status_code, 302)
            finally:
                server.shutdown()
                thread.join()
        self.assertEqual([key for key, _ in attempts], [reply_id, reply_id])
        self.assertEqual(attempts[0][1], attempts[1][1])
        self.assertEqual(attempts[0][1]["staff_id"], agent)
        self.cur.execute("""
            SELECT count(*),bool_and(sent_at IS NOT NULL) FROM ticket_message
            WHERE ticket_uuid=%s AND sender_type='staff' AND delivery_status='delivered'
        """, (ticket,))
        self.assertEqual(self.cur.fetchone(), (1, True))
        self.cur.execute("SELECT completed_by::text,delivery_uuid::text FROM voc.ticket_answer_completion WHERE ticket_uuid=%s", (ticket,))
        self.assertEqual(self.cur.fetchone(), (agent, reply_id))

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
        self.assertEqual([m["ticket_uuid"] for m in data["messages"]], [a,b])
        self.assertEqual([c["ticket_uuid"] for c in data["duplicate_candidates"]], [discord])
        self.assertEqual([c["ticket_uuid"] for c in detail(discord)["duplicate_candidates"]], [a])
        self.assertEqual(detail(b)["duplicate_candidates"], [])
        with self.assertRaises(psycopg2.Error):
            self.cur.execute("INSERT INTO ticket_duplicate_candidate VALUES (%s,%s)", sorted((a,self.tickets[-1])))
        self.assertEqual([row["ticket_uuid"] for row in detail(a)["duplicate_candidates"]], [discord])
        self.assertEqual(data["ticket"]["sentiment_status"], "failed")
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
        detail = browser.get(f"/tickets/{self.tickets[1]}/")
        self.assertContainsHTML(detail, "배송 요청 1")
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

    def test_visible_predictions_and_confirmed_completion(self):
        import re
        from django.core import signing
        from apps.tickets.services import DISPLAY_SALT

        self.login()
        ticket = self.tickets[0]
        agent = self.employees[0]
        first_category, final_category, attempt = (str(uuid4()) for _ in range(3))
        self.cur.execute("UPDATE ticket SET classification_final_at=NULL WHERE ticket_uuid=%s", (ticket,))
        self.assertEqual(self.client.get(f"/api/tickets/{ticket}").status_code, 404)
        self.cur.execute("""
            INSERT INTO category(category_uuid,domain_uuid,name)
            SELECT %s::uuid,domain_uuid,'미분류' FROM ticket WHERE ticket_uuid=%s
            UNION ALL SELECT %s::uuid,domain_uuid,'환불' FROM ticket WHERE ticket_uuid=%s
        """, (first_category, ticket, final_category, ticket))
        self.cur.execute("""
            INSERT INTO voc.inference_attempt(attempt_id,ticket_uuid,api,traffic,preferred_route,model_name,model_version,rollout,ended_at)
            VALUES (%s,%s,'classification','operational','stable','bert','1','stable',clock_timestamp())
        """, (attempt, ticket))
        self.cur.execute("INSERT INTO voc.ticket_prediction VALUES (%s,'category',%s,%s)", (ticket, first_category, attempt))
        self.cur.execute("""
            UPDATE ticket SET category_uuid=%s,category_status='succeeded',classification_final_at=clock_timestamp()
            WHERE ticket_uuid=%s
        """, (first_category, ticket))
        self.cur.execute("INSERT INTO staff_permission SELECT %s,permission_uuid FROM permission WHERE code='ticket.handle'", (agent,))
        response = self.client.get(f"/tickets/{ticket}/")
        self.assertContainsHTML(response, "미분류")
        self.assertContainsHTML(response, "분류 실패")
        signed = re.search(r'name="token" value="([^"]+)"', response.text)[1]
        snapshot = signing.loads(signed, salt=DISPLAY_SALT)
        self.assertEqual(len(snapshot["predictions"]), 1)
        self.cur.execute("SELECT count(*) FROM voc.prediction_display WHERE ticket_uuid=%s", (ticket,))
        self.assertEqual(self.cur.fetchone()[0], 0)
        for _ in range(2):
            self.assertEqual(self.client.post("/api/tickets/predictions/displayed", {
                "token": signed, "displayed": [ticket + ":category", ticket + ":sentiment"],
            }).status_code, 204)
        self.assertEqual(self.client.post(f"/tickets/{ticket}/classification/", {
            "category": final_category, "sentiment": "",
        }).status_code, 302)
        self.cur.execute("""
            INSERT INTO ticket_message(ticket_uuid,staff_user_uuid,body,sender_type,message_type,delivery_status,created_at)
            VALUES (%s,%s,'확인했습니다.','staff','reply','failed',clock_timestamp()) RETURNING ticket_message_uuid
        """, (ticket, agent))
        delivery = self.cur.fetchone()[0]
        self.assertEqual(self.client.post(f"/tickets/{ticket}/complete/", {"delivery": delivery}).status_code, 409)
        self.cur.execute("SELECT count(*) FROM voc.ticket_answer_completion WHERE ticket_uuid=%s", (ticket,))
        self.assertEqual(self.cur.fetchone()[0], 0)
        self.cur.execute("UPDATE ticket_message SET delivery_status='delivered' WHERE ticket_message_uuid=%s", (delivery,))
        for _ in range(2):
            self.assertEqual(self.client.post(f"/tickets/{ticket}/complete/", {"delivery": delivery}).status_code, 302)
        self.cur.execute("SELECT kind,displayed_value,final_value,model_version FROM monitor.modification")
        self.assertEqual(self.cur.fetchall(), [("category", first_category, final_category, "1")])

    def assertContainsHTML(self, response, content):
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(content, response.text)
