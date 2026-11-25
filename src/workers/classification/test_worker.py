import os
import unittest
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

import worker


class ProtocolTests(unittest.TestCase):
    def test_rejects_undocumented_labels_wrong_version_and_wrong_correlation(self):
        job = {"model_url": "http://fixture", "request_id": uuid4(), "domain_id": str(uuid4()),
               "analysis_kind": "sentiment", "model_version": "stub-v1", "model_text": "샘플"}
        valid = {"label": "중립", "analysis_kind": "sentiment", "model_version": "stub-v1",
                 "request_id": str(job["request_id"])}
        for change, code in [({"label": "neutral"}, "invalid_label"), ({"model_version": "other"}, "model_version_mismatch"),
                             ({"request_id": "mismatch"}, "response_mismatch")]:
            with self.subTest(code=code), patch.object(worker, "urlopen", return_value=BytesIO(
                    worker.json.dumps({**valid, **change}).encode())):
                with self.assertRaises(worker.PredictionError) as raised:
                    worker.predict(job)
                self.assertEqual(raised.exception.code, code)


@unittest.skipUnless(os.getenv("VOC_CLASSIFICATION_TEST_ADMIN_DSN"), "Requires an isolated pgvector test server")
class WorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn = os.environ["VOC_CLASSIFICATION_TEST_ADMIN_DSN"]
        database = "voc_classification_test_" + uuid4().hex
        admin = psycopg.connect(dsn, autocommit=True)
        cls.addClassCleanup(admin.close)
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        cls.addClassCleanup(admin.execute, sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))
        cls.cn = psycopg.connect(dsn, dbname=database, autocommit=True, row_factory=dict_row)
        cls.addClassCleanup(cls.cn.close)
        src = Path(__file__).resolve().parents[2]
        for path in [src/"collector/schema.sql", src/"collector/ticket_schema.sql", Path(worker.__file__).with_name("schema.sql")]:
            with cls.cn.transaction():
                cls.cn.execute(path.read_text())

    def setUp(self):
        self.company, self.domain, channel, self.category = (uuid4() for _ in range(4))
        self.config = worker.Config(self.company, 100, worker.Release("v1","http://v1"), worker.Release("v2","http://v2"))
        self.cn.execute("INSERT INTO domain VALUES (%s,'시험')", (self.domain,))
        self.cn.execute("INSERT INTO company(company_uuid,email_domain,domain_uuid) VALUES(%s,'qa.test',%s)",
                        (self.company,self.domain))
        self.cn.execute("INSERT INTO channel VALUES (%s,%s,'qa','gmail')", (channel,self.company))
        self.cn.execute("INSERT INTO voc.ticket_source_connection VALUES(%s,'gmail','qa','',%s,%s)",
                        (str(self.company),self.company,channel))
        self.cn.execute("INSERT INTO category VALUES (%s,%s,'배송')", (self.category,self.domain))
        self.cn.execute("INSERT INTO voc.classification_category_map VALUES (%s,'delivery',%s)", (self.domain,self.category))
        self.event = uuid4()
        self.cn.execute("""
            INSERT INTO voc.bronze_messages VALUES (%s,%s,'gmail','qa','m1','synthetic',now(),'{}','{}')
        """, (self.event,str(self.company)))
        self.cn.execute("""
            INSERT INTO voc.silver_messages(schema_version,event_id,company_id,channel,connection_id,source_message_id,source_conversation_id,
                message_kind,sender_ref,body_text,body_version,received_at,source_metadata)
            VALUES (4,%s,%s,'gmail','qa','m1','thread','customer_inquiry','synthetic@example.test','첫 본문',1,now(),'{"label_ids":[],"to_addresses":[]}')
        """, (self.event,str(self.company)))

    def jobs(self):
        return self.cn.execute("SELECT * FROM voc.classification_job WHERE company_uuid=%s ORDER BY analysis_kind",
                               (self.company,)).fetchall()

    def ticket(self):
        return self.cn.execute("SELECT * FROM ticket WHERE source_event_id=%s", (self.event,)).fetchone()

    def two_claims(self):
        config = self.config
        self.assertTrue(worker.dispatch(self.cn, config))
        self.assertTrue(worker.dispatch(self.cn, config))
        self.assertFalse(worker.dispatch(self.cn, config))
        claimed = [worker.claim(self.cn, config)[1] for _ in range(2)]
        return {job["analysis_kind"]: job for job in claimed}

    def test_two_independent_routes_idempotent_redelivery_and_tenant_isolation(self):
        self.assertFalse(worker.dispatch(self.cn, replace(self.config, company=uuid4())))
        claimed = self.two_claims()
        self.assertEqual(claimed["sentiment"]["model_version"], "v2")
        self.assertEqual(claimed["category"]["model_version"], "v1")
        self.assertEqual(claimed["sentiment"]["model_text"], "첫 본문")
        self.assertEqual(self.ticket()["category_status"], "running")
        worker.finish(self.cn, claimed["category"], "delivery")
        worker.finish(self.cn, claimed["sentiment"], "긍정")
        self.cn.execute("UPDATE voc.ticket_analysis_outbox SET published_at=NULL WHERE company_uuid=%s", (self.company,))
        worker.dispatch(self.cn, replace(self.config,percent=0))
        self.assertIsNone(worker.claim(self.cn, self.config)[1])
        self.assertEqual(len(self.jobs()), 2)
        self.assertEqual(self.ticket()["category_uuid"], self.category)
        self.assertEqual(self.ticket()["sentiment"], "긍정")
        self.assertTrue(all(job["applied"] and job["state"]=="succeeded" for job in self.jobs()))

    def test_expired_attempt_fenced_and_retry_uses_original_release(self):
        item = self.two_claims()["sentiment"]
        self.cn.execute("UPDATE voc.classification_job SET ready_at=now() WHERE request_id=%s", (item["request_id"],))
        _, retry = worker.claim(self.cn, replace(self.config,percent=0))
        self.assertEqual((retry["request_id"], retry["model_version"], retry["attempt_count"]), (item["request_id"],"v2",2))
        worker.finish(self.cn, item, "부정")
        self.assertIsNone(self.ticket()["sentiment"])
        worker.finish(self.cn, retry, "중립")
        self.assertEqual(self.ticket()["sentiment"], "중립")
        history = self.cn.execute("SELECT outcome FROM voc.classification_attempt WHERE request_id=%s ORDER BY attempt_no",
                                  (item["request_id"],)).fetchall()
        self.assertEqual([r["outcome"] for r in history], ["expired", "succeeded"])

    def test_manual_correction_and_new_revision_are_preserved(self):
        old = self.two_claims()
        self.cn.execute("UPDATE ticket SET category_uuid=%s,category_status='succeeded' WHERE source_event_id=%s",
                        (self.category,self.event))
        worker.finish(self.cn, old["category"], None)
        self.assertEqual(self.ticket()["category_uuid"], self.category)
        self.assertFalse(self.jobs()[0]["applied"])
        self.cn.execute("UPDATE voc.silver_messages SET body_version=2,body_text='정정 본문' WHERE event_id=%s", (self.event,))
        worker.finish(self.cn, old["sentiment"], "부정")
        self.assertIsNone(self.ticket()["sentiment"])
        with patch.object(worker, "predict", return_value="중립") as predict:
            while worker.process_one(self.cn, self.config):
                pass
        self.assertEqual(predict.call_count, 1)
        self.assertEqual(predict.call_args.args[0]["model_text"], "정정 본문")
        self.assertEqual(self.ticket()["sentiment"], "중립")
        self.assertEqual(self.ticket()["category_uuid"], self.category)

    def test_withdrawn_input_rechecks_role_before_write_and_resumes_on_confirmation(self):
        job = self.two_claims()["sentiment"]
        self.cn.execute("UPDATE voc.silver_messages SET message_kind='unknown' WHERE event_id=%s", (self.event,))
        worker.finish(self.cn, job, "부정")
        self.assertIsNone(self.ticket()["sentiment"])
        self.assertFalse(worker.process_one(self.cn, self.config))
        self.cn.execute("UPDATE voc.silver_messages SET message_kind='customer_inquiry' WHERE event_id=%s", (self.event,))
        _, resumed = worker.claim(self.cn, self.config)
        self.assertEqual(resumed["request_id"], job["request_id"])
        worker.finish(self.cn, resumed, "긍정")
        self.assertEqual(self.ticket()["sentiment"], "긍정")

    def test_unmapped_category_fails_after_retries_and_abstention_is_a_valid_success(self):
        job = self.two_claims()["category"]
        for number in range(worker.MAX_ATTEMPTS):
            worker.finish(self.cn, job, "unknown-code")
            if number < worker.MAX_ATTEMPTS-1:
                self.cn.execute("UPDATE voc.classification_job SET ready_at=now() WHERE request_id=%s", (job["request_id"],))
                _, job = worker.claim(self.cn, self.config)
        self.assertEqual(self.ticket()["category_status"], "failed")
        self.cn.execute("UPDATE voc.silver_messages SET body_version=2,body_text='새 질문' WHERE event_id=%s", (self.event,))
        worker.dispatch(self.cn, self.config)
        _, revised = worker.claim(self.cn, self.config)
        self.assertEqual(revised["analysis_kind"], "category")
        worker.finish(self.cn, revised, None)
        self.assertEqual(self.ticket()["category_status"], "succeeded")
        self.assertIsNone(self.ticket()["category_uuid"])


if __name__ == "__main__":
    unittest.main()
