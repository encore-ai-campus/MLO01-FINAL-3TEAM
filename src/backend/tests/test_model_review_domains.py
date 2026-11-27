import os
from unittest import TestCase, skipUnless
from uuid import uuid4

from django.db import connection, transaction
from django.test import Client
from psycopg2.extras import Json

from apps.accounts.repository import token_digest
from apps.preferences import model_review


@skipUnless(os.environ.get("VOC_MODEL_POLICY_TEST_DB") == "1", "Requires TiCats model review schema")
class CompanyReviewDomainsTests(TestCase):
    path = "/model-training/review/"

    def setUp(self):
        self.enterContext(transaction.atomic())
        self.addCleanup(transaction.set_rollback, True)
        self.cursor = self.enterContext(connection.cursor())
        self.company = self.make_company()
        self.other = self.make_company()

    def make_company(self):
        company = uuid4()
        self.cursor.execute(
            "INSERT INTO public.company(company_uuid,email_domain) VALUES (%s,%s)",
            (company, f"{company.hex}.test"),
        )
        return company

    def signed_in(self, role):
        staff, token = uuid4(), uuid4().hex
        self.cursor.execute(
            "INSERT INTO public.staff_user(staff_user_uuid,company_uuid,email,password_hash,role) "
            "VALUES (%s,%s,%s,'!',%s)",
            (staff, self.company, f"test@{self.company.hex}.test", role),
        )
        self.cursor.execute(
            "INSERT INTO public.staff_session(token_hash,staff_user_uuid,expires_at) "
            "VALUES (%s,%s,now()+ interval '1 hour')", (token_digest(token), staff),
        )
        client = Client()
        client.cookies["voc_desk_session"] = token
        return client

    def test_company_selection_and_domain_policy_survive_disabling_domain(self):
        common = model_review.read(self.company, "sentiment")
        other_before = model_review.read(self.other, "sentiment")
        selected = ["commerce", "finance", "food_delivery"]
        model_review.save_domains(self.company, selected, common["domain_revision"])
        finance = model_review.read(self.company, "category", domain="finance")
        baseline = model_review.read(self.company, "category", domain="food_delivery")
        self.assertEqual({r["code"] for r in finance["labels"]}, {
            "card_loss", "balance_limit_inquiry", "transaction_history_inquiry", "transfer_inquiry",
        })
        model_review.save(self.company, "category", {
            "revision": finance["policy"]["revision"], "recall__card_loss": "97",
        }, domain="finance")
        self.assertEqual(model_review.read(self.company, "sentiment")["policy"], common["policy"])
        self.assertEqual(model_review.read(self.company, "category", domain="food_delivery")["labels"], baseline["labels"])
        with self.assertRaises(model_review.PolicyConflict):
            model_review.save_domains(self.company, [], common["domain_revision"])
        model_review.save_domains(self.company, [], finance["domain_revision"])
        after = model_review.read(self.company, "sentiment")
        self.assertEqual([i["id"] for i in after["scopes"]], ["sentiment"])
        client = self.signed_in("super_admin")
        self.assertIn(client.get(self.path, {"scope": "category", "domain": "finance"}).status_code, (400, 403, 404))
        self.assertIn(client.post(self.path, {"scope": "category", "domain": "finance", "action": "save",
                                             "revision": 1, "recall__card_loss": 12}).status_code, (400, 403, 404))
        model_review.save_domains(self.company, selected, after["domain_revision"])
        finance_again = model_review.read(self.company, "category", domain="finance")
        self.assertEqual(next(r["min_recall"] for r in finance_again["labels"] if r["code"] == "card_loss"), 97)
        self.assertEqual(model_review.read(self.other, "sentiment")["domains"], other_before["domains"])

    def test_staff_cannot_select_domains_and_other_companies_benchmarks_are_unavailable(self):
        staff = self.signed_in("staff")
        common = model_review.read(self.company, "sentiment")
        self.assertFalse(common["original_available"])
        response = staff.post(self.path, {
            "scope": "sentiment", "action": "save_domains",
            "domains": ["commerce", "finance"], "domain_revision": common["domain_revision"],
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(model_review.read(self.company, "sentiment")["domain_revision"], common["domain_revision"])

    def test_original_benchmark_reads_current_rules_without_changing_saved_models(self):
        codes = ["card_loss", "transaction_history_inquiry", "balance_limit_inquiry", "transfer_inquiry"]
        counts = {code: {"n": 1, "tp": 1, "predicted": 1} for code in codes}
        matrix = [[int(i == j) for j in range(4)] for i in range(4)]
        for model, model_hash in (("klue_bert_v1", "a" * 64), ("ax_encoder", "b" * 64)):
            self.cursor.execute("""
                INSERT INTO voc.model_review_benchmark(company_uuid,task_code,model_code,model_sha256,
                    report_sha256,split_sha256,row_count,macro_f1,label_counts,label_order,confusion_matrix)
                VALUES (%s,'finance',%s,%s,%s,%s,4,1,%s,%s,%s)
                """, (self.company, model, model_hash, "c" * 64, "d" * 64, Json(counts), Json(codes), Json(matrix)))
        for company in (self.company, self.other):
            revision = model_review.read(company, "sentiment")["domain_revision"]
            model_review.save_domains(company, ["finance"], revision)
        args = {"scope": "category", "domain": "finance"}
        saved = model_review.read(self.company, **args)
        visible = model_review.read(self.company, **args, pair="original")
        self.assertEqual(visible["evaluation_samples"], 4)
        self.assertTrue(visible["original_available"])
        self.assertFalse(model_review.read(self.other, **args)["original_available"])
        client = self.signed_in("super_admin")
        html = client.get(self.path, {**args, "pair": "original"})
        self.assertEqual(html.status_code, 200)
        self.assertIn("표본 부족", html.content.decode())
        self.assertEqual(saved["policy"], model_review.read(self.company, **args)["policy"])
        # Removing a gate changes its verdict, not the source model's measured recall.
        model_review.save(self.company, "category", {
            "revision": saved["policy"]["revision"], "labels_present": "1",
            **{"enabled__" + code: "on" for code in codes if code != "card_loss"},
        }, domain="finance")
        updated = model_review.read(self.company, **args, pair="original")
        disabled = next(row for row in updated["rows"] if row.get("label_code") == "card_loss")
        self.assertEqual((disabled["status"], disabled["stable"], disabled["candidate"]),
                         ("미적용", "100% (1/1건)", "100% (1/1건)"))
