"""Verify event-time counts and immutable input versions in a disposable database."""

import os
import unittest
from datetime import timedelta

from .datasets import database
from .smoke_fixture import receive, seed


@unittest.skipUnless(os.environ.get("VOC_TRAINING_TEST_DSN"), "Requires an isolated training database")
class HistoryTests(unittest.TestCase):
    def test_upper_and_lower_reply_boundaries_and_new_body_version(self):
        self.assertEqual(os.environ["VOC_TRAINING_TEST_DSN"], os.environ["DATABASE_URL"])
        with database() as cn, cn.transaction(force_rollback=True):
            company = seed(cn)["company_uuid"]
            at = cn.execute("SELECT clock_timestamp() - interval '1 day' AS t").fetchone()["t"]
            cn.execute("UPDATE voc.training_capture SET enabled_at=%s", (at-timedelta(days=10),))
            sender = "clock-test@example.test"

            def earlier(days, first_sent=None, closed_at=None):
                row = receive(cn, company, "gmail", sender, None, f"이전 문의 {days}",
                              received_at=at-timedelta(days=days))
                cn.execute("UPDATE voc.model_text_history SET available_at=available_at-interval '10 days' WHERE event_id=%s",
                           (row["silver_event_id"],))
                if first_sent:
                    cn.execute("""
                        INSERT INTO public.ticket_message(ticket_uuid,body,sender_type,message_type,
                            delivery_status,created_at,sent_at)
                        VALUES (%s,'답변','staff','reply','sent',%s,%s)
                    """, (row["ticket_uuid"], first_sent, first_sent))
                if closed_at:
                    cn.execute("""
                        INSERT INTO public.ticket_event(ticket_uuid,event_type_uuid,from_status,to_status,created_at)
                        SELECT %s,event_type_uuid,'미처리','답변완료',%s FROM public.event_type
                        WHERE code='ticket_reply_completed'
                    """, (row["ticket_uuid"],closed_at))
                return row

            valid = earlier(8, at-timedelta(days=7), at-timedelta(days=7))
            earlier(5, closed_at=at-timedelta(minutes=1))
            at_boundary = earlier(3, at, at)
            after = earlier(2, at+timedelta(minutes=1), at+timedelta(minutes=1))
            row = receive(cn, company, "gmail", sender, None, "새 문의", received_at=at)
            self.assertEqual(row["unresolved_count"], 2)
            self.assertEqual(row["avg_first_reply_7d_seconds"], 86400)
            sources = row["calculation_sources"]
            self.assertEqual({entry["ticket_uuid"] for entry in sources["unresolved"]},
                             {str(at_boundary["ticket_uuid"]),str(after["ticket_uuid"])})
            self.assertEqual([entry["ticket_uuid"] for entry in sources["first_replies"]],
                             [str(valid["ticket_uuid"])])

            cn.execute("""
                UPDATE voc.silver_messages SET body_text='수정된 정제 본문',body_version=2 WHERE event_id=%s
            """, (row["silver_event_id"],))
            versions = cn.execute("""
                SELECT * FROM voc.feature_snapshot WHERE ticket_uuid=%s ORDER BY body_version
            """, (row["ticket_uuid"],)).fetchall()
            self.assertEqual([item["body"] for item in versions], ["새 문의", "수정된 정제 본문"])
            self.assertNotEqual(versions[0]["feature_uuid"], versions[1]["feature_uuid"])
            self.assertTrue(all(item["calculation_sources"] == sources for item in versions))
