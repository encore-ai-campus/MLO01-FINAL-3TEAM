import json
import os
import re
from dataclasses import replace
from datetime import timedelta, timezone as datetime_timezone
from io import StringIO
from unittest.mock import patch
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.utils import timezone

from apps.chat.training import (
    ChatClosed, ChatDisplay, ChatEdit, ChatInput, ChatLabel, ChatPrediction, record_chat_end,
)
from config.http import DeskError
from tests.test_tickets import SRC, TicketDatabase

KEPT = "상담사 확인 후 유지"
EDITED = "상담사 직접 수정·지정"


class TrainingCandidateTests(TicketDatabase):
    def allow_reply(self, employee):
        self.cur.execute("""
            INSERT INTO staff_permission SELECT %s,permission_uuid FROM permission WHERE code='ticket.reply'
            ON CONFLICT DO NOTHING
        """, (employee,))

    def ticket_scope(self, ticket):
        self.cur.execute("SELECT company_uuid,domain_uuid FROM ticket WHERE ticket_uuid=%s", (ticket,))
        return self.cur.fetchone()

    def make_category(self, ticket):
        self.cur.execute("""
            INSERT INTO category(domain_uuid,name) SELECT domain_uuid,'연결 확인' FROM ticket WHERE ticket_uuid=%s
            RETURNING category_uuid
        """, (ticket,))
        return self.cur.fetchone()[0]

    def prediction(self, ticket, sentiment="중립", *, category=None, traffic="operational", late=False,
                   preprocess_version=1):
        attempt = str(uuid4())
        self.cur.execute("""
            INSERT INTO voc.inference_attempt(
                attempt_id,ticket_uuid,body_version,preprocess_version,api,traffic,preferred_route,
                model_name,model_version,rollout,ended_at
            ) VALUES (%s,%s,1,%s,'classification',%s,'stable','test-model','1','stable',
                      clock_timestamp()+%s*interval '1 day')
        """, (attempt, ticket, preprocess_version, traffic, int(late)))
        for kind, value in (("sentiment", sentiment), ("category", category)):
            if value:
                self.cur.execute("INSERT INTO voc.ticket_prediction VALUES (%s,%s,%s,%s)", (ticket, kind, value, attempt))
        self.cur.execute("UPDATE ticket SET sentiment=%s,category_uuid=%s WHERE ticket_uuid=%s",
                         (sentiment, category, ticket))
        return attempt

    def display_form(self, ticket, *fields):
        response = self.client.get(f"/tickets/{ticket}/")
        self.assertEqual(response.status_code, 200, response.text)
        token = re.search(r'name="token" value="([^"]+)"', response.text)[1]
        return {"display_token": token, "displayed": [ticket + ":" + field for field in fields]}

    def complete(self, ticket, employee, form=None, *, message_type="reply", status="delivered"):
        self.cur.execute("""
            INSERT INTO ticket_message(ticket_uuid,staff_user_uuid,body,sender_type,message_type,delivery_status,created_at)
            VALUES (%s,%s,'격리 검증','staff',%s,%s,clock_timestamp()) RETURNING ticket_message_uuid
        """, (ticket, employee, message_type, status))
        delivery = self.cur.fetchone()[0]
        response = self.client.post(f"/tickets/{ticket}/complete/", {"delivery": delivery, **(form or {})})
        return response, delivery

    def candidates(self, ticket):
        self.cur.execute("""
            SELECT channel,kind,value,origin,ticket_attempt_uuid::text,input_version,preprocess_version
            FROM voc.retraining_candidate WHERE ticket_uuid=%s ORDER BY kind
        """, (ticket,))
        return self.cur.fetchall()

    def rows_command(self, company, *, limit=100):
        output = StringIO()
        call_command("training_candidates", company, limit=limit, stdout=output)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_gmail_completion_records_only_proven_fields_even_with_pending_display(self):
        ticket, employee = self.tickets[0], self.employees[0]
        company, domain = self.ticket_scope(ticket)
        category = self.make_category(ticket)
        attempt = self.prediction(ticket)
        self.allow_reply(employee)
        self.login()
        old_tab = timezone.now() - timedelta(minutes=11)
        with patch("django.core.signing.time.time", return_value=old_tab.timestamp()):
            form = self.display_form(ticket, "sentiment")
        first_report = self.client.post("/api/tickets/predictions/displayed", {
            "token": form["display_token"], "displayed": form["displayed"],
        })
        self.assertEqual(first_report.status_code, 422, first_report.text)
        response = self.client.post(f"/tickets/{ticket}/classification/", {"category": category})
        self.assertEqual(response.status_code, 302, response.text)
        self.assertEqual(self.candidates(ticket), [])
        reply_id = str(uuid4())
        url = f"/tickets/{ticket}/reply/"
        payload = {"reply_id": reply_id, "body": "격리 검증", **form}
        with patch.dict(os.environ, {"VOC_REPLY_URL": "http://test.invalid/reply"}), patch(
                "apps.tickets.reply.sent_to_provider", side_effect=[None, "confirmed"]) as provider:
            self.assertEqual(self.client.post(url, payload).status_code, 502)
            self.assertEqual(self.candidates(ticket), [])
            self.assertEqual(self.client.post(url, payload).status_code, 302)
            self.assertEqual(self.client.post(url, payload).status_code, 302)
            self.assertEqual(provider.call_count, 2)
        self.assertEqual(self.candidates(ticket), [
            ("gmail", "category", category, EDITED, None, 1, 1),
            ("gmail", "sentiment", "중립", KEPT, attempt, 1, 1),
        ])
        rows = [row for row in self.rows_command(company) if row["ticket_uuid"] == ticket]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual((row["domain_uuid"], row["delivery_uuid"], row["completed_by"],
                              row["label_staff_uuid"]), (domain, reply_id, employee, employee))
        self.assertEqual(len(self.rows_command(company, limit=1)), 1)
        self.assertEqual(self.rows_command(self.ticket_scope(self.tickets[-1])[0]), [])
        self.cur.execute("""
            SELECT kind,displayed_value,final_value FROM monitor.modification
            WHERE completed_at=(SELECT completed_at FROM voc.ticket_answer_completion WHERE ticket_uuid=%s)
        """, (ticket,))
        self.assertEqual(self.cur.fetchall(), [("sentiment", "중립", "중립")])
        self.cur.execute("SELECT count(*) FROM voc.evaluation_label")
        self.assertEqual(self.cur.fetchone()[0], 0)

    def test_discord_restored_prediction_classifies_as_kept(self):
        ticket, employee = self.tickets[2], self.employees[0]
        category = self.make_category(ticket)
        attempt = self.prediction(ticket, category=category)
        self.allow_reply(employee)
        self.login()
        form = self.display_form(ticket, "sentiment")
        for value in ("긍정", "중립"):
            response = self.client.post(f"/tickets/{ticket}/classification/", {"sentiment": value, **form})
            self.assertEqual(response.status_code, 302)
        response, delivery = self.complete(ticket, employee)
        self.assertEqual(response.status_code, 302, response.text)
        self.assertEqual(self.client.post(f"/tickets/{ticket}/complete/", {"delivery": delivery}).status_code, 302)
        self.assertEqual(self.candidates(ticket), [("discord", "sentiment", "중립", KEPT, attempt, 1, 1)])

    def test_missing_blank_late_replayed_or_other_staff_display_does_not_create_candidate(self):
        employee = self.employees[1]
        company, _ = self.ticket_scope(self.tickets[3])
        self.allow_reply(employee)
        self.cur.execute("""
            INSERT INTO staff_user(company_uuid,email,password_hash)
            SELECT company_uuid,'agent2@qa2.test',password_hash FROM staff_user WHERE staff_user_uuid=%s
            RETURNING staff_user_uuid
        """, (employee,))
        other = self.cur.fetchone()[0]
        self.cur.execute("INSERT INTO staff_permission SELECT %s,permission_uuid FROM permission WHERE code='ticket.read'",
                         (other,))
        self.login(2)
        options = {
            "missing": {}, "blank": {}, "late_prediction": {"late": True}, "replay": {"traffic": "replay"},
            "different_staff": {}, "late_display": {}, "changed_input": {},
        }
        for (scenario, kwargs), ticket in zip(options.items(), self.tickets[3:]):
            with self.subTest(scenario=scenario):
                self.prediction(ticket, **kwargs)
                form = self.display_form(ticket, "sentiment")
                display_by_finisher = form
                if scenario in ("missing", "late_display"):
                    display_by_finisher = None
                elif scenario == "blank":
                    self.assertEqual(self.client.post(f"/tickets/{ticket}/classification/", {"sentiment": "", **form}).status_code, 302)
                elif scenario == "different_staff":
                    self.login(2, 2)
                    other_form = self.display_form(ticket, "sentiment")
                    self.assertEqual(self.client.post("/api/tickets/predictions/displayed", {
                        "token": other_form["display_token"], "displayed": other_form["displayed"],
                    }).status_code, 204)
                    self.login(2)
                    display_by_finisher = other_form
                elif scenario == "changed_input":
                    page = self.client.get(f"/tickets/{ticket}/")
                    stale = re.search(r'name="classification_snapshot" value="([^"]+)"', page.text)[1]
                    self.cur.execute("""
                        UPDATE voc.silver_messages SET body_version=body_version+1 WHERE event_id=(
                            SELECT source_event_id FROM ticket WHERE ticket_uuid=%s
                        )
                    """, (ticket,))
                    response = self.client.post(f"/tickets/{ticket}/classification/", {
                        "sentiment": "긍정", "classification_snapshot": stale,
                    })
                    self.assertEqual(response.status_code, 409, response.text)
                response, _ = self.complete(ticket, employee, display_by_finisher)
                self.assertEqual(response.status_code, 302, response.text)
                if scenario == "late_display":
                    self.assertEqual(self.client.post("/api/tickets/predictions/displayed", {
                        "token": form["display_token"], "displayed": form["displayed"],
                    }).status_code, 204)
                self.assertEqual(self.candidates(ticket), [])
        self.assertEqual(self.rows_command(company), [])

    def test_memo_or_failed_reply_is_never_a_completion(self):
        ticket, employee = self.tickets[1], self.employees[0]
        self.prediction(ticket)
        self.allow_reply(employee)
        self.login()
        form = self.display_form(ticket, "sentiment")
        for message_type, status in (("memo", "delivered"), ("reply", "failed")):
            response, _ = self.complete(ticket, employee, form, message_type=message_type, status=status)
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(self.candidates(ticket), [])
        self.cur.execute("SELECT count(*) FROM voc.ticket_answer_completion WHERE ticket_uuid=%s", (ticket,))
        self.assertEqual(self.cur.fetchone()[0], 0)

    def test_chat_keeps_only_a_completion_agents_exact_operational_prediction(self):
        ticket = self.tickets[0]
        company, domain = self.ticket_scope(ticket)
        other_company_staff = UUID(self.employees[1])
        self.cur.execute("""
            INSERT INTO staff_user(company_uuid,email,password_hash)
            SELECT company_uuid,'agent3@qa1.test',password_hash FROM staff_user WHERE staff_user_uuid=%s
            RETURNING staff_user_uuid
        """, (self.employees[0],))
        employee = self.cur.fetchone()[0]
        self.login(employee=3)
        token = self.client.cookies["voc_session"].value
        completed_at = timezone.now().astimezone(ZoneInfo("Asia/Seoul"))
        input_snapshot = ChatInput(uuid4(), 2, "a" * 64)
        prediction = ChatPrediction(uuid4(), input_snapshot, "긍정", "operational", "test-model", "1",
                                    "stable", completed_at - timedelta(seconds=3))
        seen = ChatDisplay(UUID(employee), prediction.attempt_uuid, "긍정", completed_at - timedelta(seconds=2))
        category = self.make_category(ticket)
        edit = ChatEdit(UUID(employee), input_snapshot, category, completed_at - timedelta(seconds=1))
        label = ChatLabel("긍정", prediction, seen)
        event = ChatClosed(uuid4(), uuid4(), UUID(company), UUID(domain), UUID(employee), completed_at, input_snapshot, {
            "sentiment": label, "category": ChatLabel(category, edit=edit),
        })
        self.assertTrue(record_chat_end(token, event))
        self.assertFalse(record_chat_end(token, event))
        utc = datetime_timezone.utc
        self.assertFalse(record_chat_end(token, replace(event, completed_at=completed_at.astimezone(utc), labels={
            "sentiment": replace(label, prediction=replace(prediction, ended_at=prediction.ended_at.astimezone(utc)),
                                 display=replace(seen, displayed_at=seen.displayed_at.astimezone(utc))),
            "category": ChatLabel(category, edit=replace(edit, edited_at=edit.edited_at.astimezone(utc))),
        })))
        with self.assertRaises(DeskError) as denied:
            record_chat_end(token, replace(event, completed_by=other_company_staff))
        self.assertEqual(denied.exception.code, "CHAT_COMPLETION_FORBIDDEN")
        self.cur.execute("""
            INSERT INTO company(email_domain,domain_uuid) VALUES ('shared-domain.test',%s) RETURNING company_uuid
        """, (domain,))
        another_company = UUID(self.cur.fetchone()[0])
        with self.assertRaises(DeskError) as denied:
            record_chat_end(token, replace(event, completion_uuid=uuid4(), company_uuid=another_company))
        self.assertEqual(denied.exception.code, "CHAT_COMPLETION_FORBIDDEN")
        with self.assertRaises(DeskError) as conflict:
            record_chat_end(token, replace(event, labels={}))
        self.assertEqual(conflict.exception.code, "CHAT_COMPLETION_CONFLICT")
        with self.assertRaises(DeskError) as denied:
            record_chat_end(token, replace(event, completion_uuid=uuid4(), labels={
                "category": ChatLabel(category, edit=replace(edit, staff_uuid=other_company_staff)),
            }))
        self.assertEqual(denied.exception.code, "INVALID_CHAT_LABEL_STAFF")

        ineligible = (
            replace(label, display=None),
            replace(label, display=replace(seen, staff_uuid=other_company_staff)),
            replace(label, display=replace(seen, displayed_at=completed_at + timedelta(seconds=1))),
            replace(label, prediction=replace(prediction, ended_at=completed_at + timedelta(seconds=1))),
            replace(label, prediction=replace(prediction, traffic="replay")),
            replace(label, prediction=replace(prediction, input=replace(input_snapshot, version=1))),
            replace(label, value=""),
        )
        for item in ineligible:
            self.assertTrue(record_chat_end(token, replace(event, completion_uuid=uuid4(), labels={"sentiment": item})))
        rows = self.rows_command(company)
        rows = [row for row in rows if row["channel"] == "chat"]
        self.assertEqual([(row["kind"], row["value"], row["origin"], row["prediction_attempt_uuid"]) for row in rows], [
            ("category", category, EDITED, None), ("sentiment", "긍정", KEPT, str(prediction.attempt_uuid)),
        ])
        self.assertEqual({(row["input_uuid"], row["input_version"], row["input_sha256"]) for row in rows},
                         {(str(input_snapshot.snapshot_uuid), 2, input_snapshot.sha256)})

    def test_upgrade_keeps_older_completion_and_backfill_requires_proof_and_apply(self):
        company, _ = self.ticket_scope(self.tickets[3])
        employee = self.employees[1]
        backed, unproven = self.tickets[12:14]
        self.allow_reply(employee)
        self.login(2)
        self.cur.execute("ALTER TABLE public.ticket DISABLE TRIGGER monitor_answer_completion")
        try:
            for ticket, seen in ((backed, True), (unproven, False)):
                self.prediction(ticket, preprocess_version=None)
                if seen:
                    form = self.display_form(ticket, "sentiment")
                    self.assertEqual(self.client.post("/api/tickets/predictions/displayed", {
                        "token": form["display_token"], "displayed": form["displayed"],
                    }).status_code, 204)
                response, delivery = self.complete(ticket, employee)
                self.assertEqual(response.status_code, 302, response.text)
                self.cur.execute("""
                    INSERT INTO voc.ticket_answer_completion(ticket_uuid,sentiment,completed_by,delivery_uuid)
                    VALUES (%s,'중립',%s,%s)
                """, (ticket, employee, delivery))
        finally:
            self.cur.execute("ALTER TABLE public.ticket ENABLE TRIGGER monitor_answer_completion")

        for _ in range(2):
            for filename in ("schema.sql", "ticket_schema.sql", "monitoring.sql"):
                self.cur.execute((SRC / "collector" / filename).read_text())
        self.assertEqual(self.candidates(backed), [])
        output = StringIO()
        call_command("backfill_training_candidates", company, stdout=output)
        self.assertEqual(output.getvalue(), "eligible=1\n")
        self.assertEqual(self.candidates(backed), [])
        for expected in (1, 0):
            output = StringIO()
            call_command("backfill_training_candidates", company, apply=True, stdout=output)
            self.assertEqual(output.getvalue(), f"inserted={expected}\n")
        self.assertEqual(len(self.candidates(backed)), 1)
        self.assertEqual(self.candidates(backed)[0][1:4], ("sentiment", "중립", KEPT))
        self.assertEqual(self.candidates(unproven), [])
