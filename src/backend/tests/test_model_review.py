import os
from copy import deepcopy
from datetime import timedelta
from unittest import TestCase, skipUnless
from uuid import uuid4

from apps.preferences import model_review


class ReplacementPolicyTests(TestCase):
    def setUp(self):
        self.policy = {field["key"]: field["default"] for field in model_review.fields("sentiment")}
        self.policy.update(baseline_uri="models:/team/1", candidate_uri="models:/team/2",
                           baseline_served="v1", candidate_served="v2")
        baseline = {"dataset": "eval-frozen", "overall": .99,
                    "counts": {"negative": {"n": 120, "tp": 117, "predicted": 120}}}
        self.models = {uri: deepcopy(baseline) for uri in ("models:/team/1", "models:/team/2")}
        self.labels = [{"name": "부정", "code": "negative", "min_recall": 90, "min_samples": 100,
                        "max_missed": None, "enabled": True}]
        lane = dict(calls=100, error_pct=1, p95=100, reviews=100, correction=10, mixed=False, pending=0)
        self.stats = {"stable": dict(lane), "canary": dict(lane, p95=150, correction=8)}

    def assess(self, scope="sentiment"):
        decision, rows = model_review.assess(self.policy, self.labels, self.models, self.stats, scope)
        return decision["code"], {row["label"]: row for row in rows}

    def test_upper_limits_are_inclusive_and_missing_optional_cost_is_visible(self):
        code, rows = self.assess()
        self.assertEqual(code, "review_expansion")
        self.assertEqual(rows["1,000호출당 추론 비용"]["candidate"], "미수집")
        self.policy["cost_limit"] = 10
        self.assertEqual(self.assess()[0], "insufficient")
        self.stats["canary"]["cost"] = 11
        self.assertEqual(self.assess()[0], "hold")
        self.stats["canary"]["cost"] = 10
        self.assertEqual(self.assess()[0], "review_expansion")

    def test_baseline_incident_and_candidate_incident_are_distinguished(self):
        self.stats["stable"]["error_pct"] = 3
        self.assertEqual(self.assess()[0], "inspect_baseline")
        self.stats["canary"]["calls"] = 0
        self.assertEqual(self.assess()[0], "inspect_baseline")
        self.assertEqual(self.assess()[1]["최근 30분 오류율"]["status"], "미달")
        self.stats["canary"]["calls"] = 100
        self.stats["stable"]["error_pct"] = 0
        self.stats["canary"]["error_pct"] = 3
        self.assertEqual(self.assess()[0], "rollback")
        self.stats["canary"].update(error_pct=0, p95=151)
        self.assertEqual(self.assess()[0], "rollback")

    def test_safety_stop_before_full_sample_and_limited_cohort(self):
        self.stats["canary"].update(calls=3, error_pct=100, streak=2)
        self.assertEqual(self.assess()[0], "insufficient")
        self.stats["canary"]["streak"] = 3
        self.assertEqual(self.assess()[0], "stop_consecutive_failures")
        self.stats["canary"]["mixed"] = True
        self.assertEqual(self.assess()[0], "mixed_versions")

    def test_only_compare_same_frozen_dataset_and_identified_served_units(self):
        self.models["models:/team/2"]["dataset"] = "later-eval"
        self.assertEqual(self.assess()[0], "different_eval")
        self.policy["candidate_served"] = ""
        self.assertEqual(self.assess()[0], "map_serving")
        self.policy["candidate_uri"] = ""
        self.assertEqual(self.assess()[0], "select_pair")

    def test_low_label_support_and_missing_subtype_metrics_prevent_recommendation(self):
        self.labels[0]["min_samples"] = 121
        code, rows = self.assess("category")
        self.assertEqual((code, rows["부정 재현율"]["status"]), ("insufficient", "표본 부족"))
        self.models["models:/team/2"]["counts"] = {}
        self.assertEqual(self.assess("category")[1]["고정 평가셋 Macro-F1"]["candidate"], "미수집")
        self.policy["min_negative"] = 500
        self.assertEqual(self.assess()[0], "insufficient")

    def test_undeployed_candidate_can_fail_the_shared_offline_quality_gate(self):
        self.policy["candidate_served"] = ""
        self.stats = {"stable": {}, "canary": {}}
        self.models["models:/team/2"]["counts"]["negative"]["tp"] = 60
        self.assertEqual(self.assess()[0], "hold")

    def test_missing_timing_or_agent_samples_never_pass(self):
        self.stats["canary"]["p95"] = None
        self.assertEqual(self.assess()[0], "insufficient")
        self.stats["canary"].update(p95=100, reviews=5)
        self.assertEqual(self.assess()[0], "insufficient")
        self.stats["canary"].update(reviews=100, pending=1)
        self.assertEqual(self.assess()[0], "insufficient")

    def test_saved_rule_changes_verdict_and_rejects_invalid_model_and_number(self):
        ctx = dict(scope="category", versions=self.models)
        model_review.draft_values({"recall__negative": "98"}, ctx, self.policy, self.labels)
        self.assertEqual(self.assess("category")[0], "hold")
        for post in ({"min_calls": "0"}, {"error_limit": "NaN"}, {"min_calls": "1.5"},
                     {"candidate_uri": "models:/another-company/1"}, {"candidate_served": "v1"}):
            with self.subTest(post=post), self.assertRaises(ValueError):
                model_review.draft_values(post, ctx, dict(self.policy), deepcopy(self.labels))


@skipUnless(os.environ.get("VOC_MODEL_POLICY_TEST_DB") == "1", "Requires local TiCats PostgreSQL")
class OperatingWindowTests(TestCase):
    def test_http_window_distinct_failures_and_cost_completeness(self):
        from django.db import connection, transaction
        from django.utils.timezone import now

        company, domain, customer, channel = (uuid4() for _ in range(4))
        with transaction.atomic(), connection.cursor() as cur:
            cur.execute("INSERT INTO domain(domain_uuid,name) VALUES (%s,'window')", (domain,))
            cur.execute("INSERT INTO company(company_uuid,email_domain,domain_uuid) VALUES (%s,'window.test',%s)",
                        (company, domain))
            cur.execute("INSERT INTO customer(customer_uuid,company_uuid,source_type,external_customer_id) "
                        "VALUES (%s,%s,'gmail','customer')", (customer, company))
            cur.execute("INSERT INTO channel(channel_uuid,company_uuid,name,type) VALUES (%s,%s,'test','gmail')",
                        (channel, company))
            at = now()
            # A 31-minute-old, slower request counts for the seven-day cost but not for latency.
            requests = [("stable", 31, 2000), ("stable", 8, 100), ("canary", 7, 120),
                        ("canary", 5, 120), ("canary", 2, 120)]
            for i, (lane, age, latency) in enumerate(requests):
                event, request = uuid4(), uuid4()
                started = at - timedelta(minutes=age)
                version = "v1" if lane == "stable" else "v2"
                cur.execute("INSERT INTO voc.bronze_messages(event_id,company_id,channel,connection_id,"
                            "source_message_id,origin,received_at,context,raw_payload) VALUES "
                            "(%s,%s,'gmail','local',%s,'test',%s,'{}','{}')", (event, str(company), str(event), started))
                cur.execute("INSERT INTO ticket(source_event_id,company_uuid,domain_uuid,channel_uuid,customer_uuid,"
                            "ticket_number,title,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,'test',%s,%s)",
                            (event, company, domain, channel, customer, f"{i:08d}", started, started))
                cur.execute("INSERT INTO voc.model_text_history(company_uuid,event_id,preprocess_version,body_version,"
                            "silver_schema_version,model_text) VALUES (%s,%s,1,1,4,'test')", (company, event))
                cur.execute("INSERT INTO voc.classification_job(request_id,company_uuid,event_id,preprocess_version,"
                            "body_version,analysis_kind,route,model_version,model_url) "
                            "VALUES (%s,%s,%s,1,1,'sentiment',%s,%s,'http://local')",
                            (request, company, event, lane, version))
                for attempt in range(1, (3 if lane == "canary" else 2)):
                    cur.execute("INSERT INTO voc.classification_attempt(request_id,attempt_no,started_at,finished_at,"
                                "outcome,http_ms,cost_krw) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                                (request, attempt, started, started + timedelta(seconds=attempt),
                                 "failed" if lane == "canary" else "succeeded", latency, None if i == 4 else .1))
            ctx = dict(map_ready=True, jobs_ready=True, scope="sentiment", domain=domain, as_of=at)
            values = dict(baseline_served="v1", candidate_served="v2")
            stats = model_review.lane_stats(cur, company, ctx, values)
            stable, canary = stats["stable"], stats["canary"]
            self.assertEqual((stable["calls"], stable["p95"], stable["cost"]), (1, 100, 100))
            self.assertEqual((canary["calls"], canary["streak"], canary["p95"], canary["cost"]), (6, 3, 120, None))
            self.assertEqual(model_review.lane_stats(cur, uuid4(), ctx, values), {"stable": {}, "canary": {}})
            transaction.set_rollback(True)
