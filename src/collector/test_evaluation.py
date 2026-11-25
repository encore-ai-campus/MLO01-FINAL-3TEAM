"""Run against an initialized PostgreSQL database with VOC_RUN_MONITORING_TESTS=1."""
import json
import os
import unittest
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
from uuid import uuid4

from . import evaluation
from .pipeline import db


class EvaluationSnapshotTests(unittest.TestCase):
    def test_only_independent_review_can_supply_fixed_evaluation_labels(self):
        label = {"id": "label", "value": "중립", "reviewer": "reviewer", "evidence": "reviewed"}
        data = {"dataset_version": "fixture", "samples": [{
            "id": "sample", "title": "검증", "text": "검증 문의", "domain_id": str(uuid4()),
            "labels": {"sentiment": label},
        }]}
        with TemporaryDirectory() as folder:
            path = Path(folder) / "snapshot.json"
            for origin in ("상담사 확인 후 유지", "상담사 직접 수정·지정", "independent_review"):
                with self.subTest(origin=origin):
                    label["origin"] = origin
                    path.write_text(json.dumps(data), encoding="utf-8")
                    if origin == "independent_review":
                        self.assertEqual(len(evaluation.checked_snapshot(path)[2]), 1)
                    else:
                        with self.assertRaises(evaluation.EvaluationError):
                            evaluation.checked_snapshot(path)


@unittest.skipUnless(os.getenv("VOC_RUN_MONITORING_TESTS") == "1", "requires initialized test PostgreSQL")
class ReplayTests(unittest.TestCase):
    def test_canary_replay_preserves_live_ticket_and_pipeline_counts(self):
        received = []
        category = uuid4()

        class Canary(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({
                    "model_name": "bert-replay-test", "model_version": "canary-1", "rollout": "canary",
                    "sentiment": "중립", "category_id": str(category), "model_ms": 1.5,
                }).encode())

            def log_message(self, *_):
                pass

        domain, company, channel, source = (uuid4() for _ in range(4))
        company_id = f"replay-test-{company}"
        with db() as cn, cn.transaction(force_rollback=True):
            cn.execute("INSERT INTO public.domain VALUES (%s,'재생 검증')", (domain,))
            cn.execute("INSERT INTO public.category VALUES (%s,%s,'미분류')", (category, domain))
            cn.execute("INSERT INTO public.company(company_uuid,email_domain,domain_uuid) VALUES (%s,'example.test',%s)",
                       (company, domain))
            cn.execute("INSERT INTO public.channel(channel_uuid,company_uuid,name,type) VALUES (%s,%s,'Gmail','gmail')",
                       (channel, company))
            cn.execute("INSERT INTO voc.ticket_source_connection VALUES (%s,'gmail','test-mailbox','',%s,%s)",
                       (company_id, company, channel))
            cn.execute("""INSERT INTO voc.bronze_messages
                          VALUES (%s,%s,'gmail','test-mailbox','test-message','replay-fixture',now(),'{}','{}')""",
                       (source, company_id))
            cn.execute("INSERT INTO voc.standardize_outbox(event_id) VALUES (%s)", (source,))
            cn.execute("""
                INSERT INTO voc.silver_messages(schema_version,event_id,company_id,channel,connection_id,source_message_id,
                    source_conversation_id,message_kind,sender_ref,title,body_text,body_version,occurred_at,received_at,source_metadata)
                VALUES (4,%s,%s,'gmail','test-mailbox','test-message','thread','customer_inquiry','customer@example.test',
                    '검증 제목','검증 본문',1,now(),now(),'{"label_ids":[],"to_addresses":[]}')
            """, (source, company_id))
            ticket_before = cn.execute("""
                SELECT ticket_uuid,status,sentiment,category_uuid,embedding,classification_final_at,ready_at
                FROM public.ticket WHERE source_event_id=%s
            """, (source,)).fetchone()
            stage_before = cn.execute("SELECT * FROM monitor.stage WHERE event_id=%s ORDER BY stage", (source,)).fetchall()
            work_before = cn.execute("""
                SELECT analysis_kind,attempt_count,finished_at FROM voc.ticket_analysis_outbox
                WHERE event_id=%s ORDER BY analysis_kind
            """, (source,)).fetchall()
            with ThreadingHTTPServer(("127.0.0.1", 0), Canary) as server:
                thread = Thread(target=server.serve_forever)
                thread.start()
                try:
                    endpoint = f"http://127.0.0.1:{server.server_port}/classification"
                    with patch.dict(os.environ, {"VOC_CLASSIFIER_CANARY_URL": endpoint}), patch.object(
                            evaluation, "db", return_value=nullcontext(cn)):
                        evaluation.replay(str(source))
                finally:
                    server.shutdown()
                    thread.join()
            self.assertEqual(received, [{
                "title": "검증 제목", "text": "검증 본문", "domain_id": str(domain),
                "kinds": ["sentiment", "category"],
            }])
            self.assertEqual(cn.execute("""
                SELECT ticket_uuid,status,sentiment,category_uuid,embedding,classification_final_at,ready_at
                FROM public.ticket WHERE source_event_id=%s
            """, (source,)).fetchone(), ticket_before)
            self.assertEqual(cn.execute(
                "SELECT * FROM monitor.stage WHERE event_id=%s ORDER BY stage", (source,)).fetchall(), stage_before)
            self.assertEqual(cn.execute("""
                SELECT analysis_kind,attempt_count,finished_at FROM voc.ticket_analysis_outbox
                WHERE event_id=%s ORDER BY analysis_kind
            """, (source,)).fetchall(), work_before)
            self.assertEqual(cn.execute("""
                SELECT traffic,rollout,model_version,error FROM voc.inference_attempt WHERE ticket_uuid=%s
            """, (ticket_before["ticket_uuid"],)).fetchall(), [{
                "traffic": "replay", "rollout": "canary", "model_version": "canary-1", "error": None,
            }])
            self.assertEqual(cn.execute("SELECT count(*) FROM monitor.normal_inquiry WHERE event_id=%s",
                                        (source,)).fetchone()["count"], 1)
            self.assertEqual(cn.execute("SELECT count(*) FROM voc.ticket_prediction WHERE ticket_uuid=%s",
                                        (ticket_before["ticket_uuid"],)).fetchone()["count"], 0)
