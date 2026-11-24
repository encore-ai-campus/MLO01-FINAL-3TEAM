"""Test the API against the real PostgreSQL schema in a newly created disposable database."""
import json
import os
import re
import unittest
from base64 import b64decode
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import psycopg2
from argon2 import PasswordHasher
from django.test import Client

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
        for filename in ("schema.sql", "ticket_schema.sql"):
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

    def login(self, company=1):
        response = self.client.post("/api/auth/login", data={"email": f"agent1@qa{company}.test", "password": "SamplePass1!"}, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.cookies["voc_session"]["httponly"])

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
        self.assertEqual([m["ticket_uuid"] for m in data["messages"]], [a,b])
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
        detail = browser.get(f"/tickets/{self.tickets[1]}/")
        self.assertContainsHTML(detail, "배송 요청 1")
        self.grant_conduct()
        detail_url = f"/tickets/{self.tickets[1]}/"
        memo = {"message_type": "memo", "body": "브라우저 메모", "request_key": str(uuid4())}
        self.addCleanup(self.cur.execute, "DELETE FROM ticket_message WHERE request_key=%s", (memo["request_key"],))
        self.assertEqual(browser.post(detail_url + "messages/", memo).status_code, 403)
        memo["csrfmiddlewaretoken"] = browser.cookies["csrftoken"].value
        saved = browser.post(detail_url + "messages/?scope=mine", memo, follow=True)
        self.assertContainsHTML(saved, "브라우저 메모")
        self.assertEqual(saved.redirect_chain, [(detail_url + "?scope=mine", 302)])
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
        self.assertContainsHTML(self.client.get(href), "고객에게 답변")
        first = self.client.post(endpoint, payload, content_type="application/json")
        again = self.client.post(endpoint, payload, content_type="application/json")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), again.json())
        self.assertEqual(first.json()["ticket_status"], "미처리")
        self.assertContainsHTML(self.client.get(path), "내부 확인 🙂")
        self.cur.execute("DELETE FROM staff_permission WHERE staff_user_uuid=%s AND permission_uuid "
                         "IN (SELECT permission_uuid FROM permission WHERE code='ticket.conduct')", (self.employees[0],))
        self.assertNotIn("내부 확인 🙂", self.client.get(path).text)
        self.assertEqual(self.client.get("/api/tickets").json()["total"], 3)
        self.cur.execute("DELETE FROM ticket_message WHERE request_key=%s", (payload["request_key"],))

    def test_reply_uses_collected_discord_route_and_completes_only_sent_ticket(self):
        self.login()
        self.grant_conduct()
        first, _, discord, *_ = self.tickets
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

    def test_gmail_failure_preserves_status_and_uses_original_headers(self):
        from apps.tickets.delivery import DeliveryError

        self.login()
        self.grant_conduct()
        ticket = self.tickets[0]
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
        self.cur.execute("""INSERT INTO staff_user(staff_user_uuid,company_uuid,email,password_hash,team_uuid)
                         VALUES (%s,%s,'receiver@qa1.test',%s,%s)""", (recipient, company, password_hash, self.team))
        self.cur.execute("INSERT INTO staff_permission SELECT %s,permission_uuid FROM permission WHERE code IN "
                         "('ticket.read','ticket.conduct')", (recipient,))
        self.cur.execute("INSERT INTO ticket_review_channel (company_uuid,name) VALUES (%s,'배송 검토') "
                         "RETURNING review_channel_uuid", (company,))
        channel = str(self.cur.fetchone()[0])
        self.cur.execute("INSERT INTO ticket_review_channel_member VALUES (%s,%s,%s)", (company, channel, recipient))
        self.addCleanup(self.cur.execute, "DELETE FROM staff_user WHERE staff_user_uuid=%s", (recipient,))
        self.addCleanup(self.cur.execute, "UPDATE ticket SET assignee_uuid=NULL WHERE ticket_uuid=%s", (ticket,))
        self.addCleanup(self.cur.execute, "DELETE FROM ticket_review_channel WHERE review_channel_uuid=%s", (channel,))
        self.addCleanup(self.cur.execute, "DELETE FROM ticket_action WHERE ticket_uuid=%s AND kind IN ('transfer','review')", (ticket,))

        options = self.client.get(f"/api/tickets/{ticket}/collaboration-options").json()
        self.assertEqual(options["transfer_recipients"][0]["staff_user_uuid"], recipient)
        transfer = self.client.post(f"/api/tickets/{ticket}/transfer",
                                    {"recipient_uuid": recipient, "reason": "배송 재확인", "attached_reply": "초안"},
                                    content_type="application/json")
        self.assertEqual(transfer.status_code, 201, transfer.text)
        self.assertEqual(transfer.json()["assigned_team_uuid"], self.team)
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
        self.assertIn({"content_type": "image/gif"}, [item["attachment"] for item in received])
        download = receiver.get(attachment_url)
        self.assertEqual(download.content, b64decode(image))
        self.assertEqual(download.headers["Content-Type"], "image/gif")
        self.assertContainsHTML(receiver.get("/inbox/?tab=review"), "확인 자료")
        self.assertContainsHTML(receiver.get("/inbox/?tab=transfer"), f"/tickets/{ticket}/")

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
            self.cur.execute("""INSERT INTO quick_reply(company_uuid,category_uuid,author_uuid,name,body,scope)
                             VALUES (%s,%s,%s,%s,'답변 본문','personal')""", (company, category, self.employees[0], name))
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

    def grant_conduct(self):
        self.cur.execute("INSERT INTO staff_permission SELECT %s,permission_uuid FROM permission WHERE code='ticket.conduct' "
                         "ON CONFLICT DO NOTHING", (self.employees[0],))
        self.addCleanup(self.cur.execute, "DELETE FROM staff_permission WHERE staff_user_uuid=%s AND permission_uuid "
                        "IN (SELECT permission_uuid FROM permission WHERE code='ticket.conduct')", (self.employees[0],))

    def assertContainsHTML(self, response, content):
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(content, response.text)
