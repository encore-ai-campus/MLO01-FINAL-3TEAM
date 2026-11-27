import os
from copy import deepcopy
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.db import connection, transaction
from django.test import Client
from django.utils import timezone

from apps.accounts.repository import token_digest
from apps.preferences import training
from config.http import DeskError


@skipUnless(os.getenv("VOC_RETRAINING_TEST_DB") == "1", "Use an isolated PostgreSQL database with the training schema")
class StopTrainingTests(TestCase):
    def setUp(self):
        self.enterContext(transaction.atomic())
        self.addCleanup(transaction.set_rollback, True)
        self.cursor = self.enterContext(connection.cursor())
        self.company, self.staff = str(uuid4()), str(uuid4())
        self.name, self.uid = "voc-training-"+uuid4().hex, str(uuid4())
        self.actor = {"company_uuid": self.company, "staff_user_uuid": self.staff, "role": "super_admin"}
        self.cursor.execute("INSERT INTO public.company(company_uuid,email_domain) VALUES (%s,'local.test')", (self.company,))
        self.cursor.execute("""INSERT INTO public.staff_user(staff_user_uuid,company_uuid,email,password_hash,name,role)
            VALUES (%s,%s,'stop@local.test','!','중지 담당','super_admin')""", (self.staff,self.company))
        self.token = uuid4().hex
        self.cursor.execute("INSERT INTO public.staff_session VALUES (%s,%s,now()+interval '1 hour')",
                            (token_digest(self.token), self.staff))
        self.client = Client()
        self.client.cookies["voc_desk_session"] = self.token
        self.path = f"/model-training/runs/{self.name}/stop/"
        self.flow = {
            "metadata": {"name": self.name,"uid": self.uid,
                         "labels": {"voc.company": self.company, "voc.requested-by": self.staff}},
            "spec": {"workflowTemplateRef": {"name": "voc-training"}, "arguments": {"parameters": [
                {"name": "company", "value": self.company}, {"name": "label-cutoff", "value": timezone.now().isoformat()}]}},
            "status": {"phase": "Running"},
        }
        self.stops = 0
        self.lose_reply = False
        self.api = self.enterContext(patch.object(training, "argo_request", side_effect=self.argo))

    def argo(self, path, payload=None, *, method=None):
        if method == "PUT":
            self.assertEqual((path, payload), (f"/{self.name}/stop", {}))
            self.stops += 1
            self.flow["spec"]["shutdown"] = "Stop"
            if self.lose_reply:
                raise OSError("lost response")
        else:
            self.assertEqual((path,method), (f"/{self.name}",None))
        return deepcopy(self.flow)

    def test_stop_delivered_once_and_terminal_confirmation_is_audited(self):
        self.assertEqual(self.client.post(self.path).status_code, 202)
        self.assertEqual(self.client.post(self.path).json()["stop"]["state"], "requested")
        self.assertEqual(self.stops, 1)
        stop = training.workflow_stop_status(self.name,self.company,self.flow,{})
        self.assertEqual((stop["state"],stop["requested_by"],stop["confirmed_at"]), ("requested","중지 담당",None))
        self.flow["status"]["phase"] = "Failed"
        final = self.client.post(self.path)
        self.assertEqual(final.status_code, 200)
        self.assertEqual(final.json()["stop"]["state"], "stopped")
        self.assertIsNotNone(final.json()["stop"]["confirmed_at"])
        self.cursor.execute("SELECT count(*),max(state) FROM voc.training_stop_request WHERE workflow_name=%s", (self.name,))
        self.assertEqual(self.cursor.fetchone(), (1,"stopped"))

    def test_lost_put_response_never_double_delivers(self):
        self.lose_reply = True
        self.assertEqual(training.request_stop(self.actor,self.name)["state"], "requested")
        self.assertEqual(training.reconcile_stop_request(self.name,self.company)["state"], "requested")
        self.assertEqual(self.stops, 1)

    def test_worker_delivers_after_network_failure_before_put(self):
        def network_down(path, payload=None, *, method=None):
            if method == "PUT":
                raise OSError("service unavailable")
            return self.argo(path, payload, method=method)
        self.api.side_effect = network_down
        self.assertEqual(training.request_stop(self.actor,self.name)["state"], "requested")
        self.assertEqual(self.stops, 0)
        self.api.side_effect = self.argo
        self.assertEqual(training.reconcile_stop_request(self.name,self.company)["state"], "requested")
        self.assertEqual(self.stops, 1)

    def test_completion_and_registration_races(self):
        self.flow["status"]["nodes"] = {"register-pod":{"type":"Pod","templateName":"register","phase":"Running"}}
        self.assertEqual(self.client.post(self.path).status_code, 409)
        self.flow["status"] = {"phase":"Succeeded"}
        self.assertEqual(self.client.post(self.path).status_code, 409)
        self.assertEqual(self.stops, 0)
        self.flow["status"] = {"phase":"Running"}
        training.request_stop(self.actor,self.name)
        self.flow["status"]["phase"] = "Succeeded"
        self.assertEqual(training.reconcile_stop_request(self.name,self.company)["state"], "completed")

    def test_tenant_role_and_csrf_are_required(self):
        self.assertEqual(self.client.get(self.path).status_code, 405)
        csrf = Client(enforce_csrf_checks=True)
        csrf.cookies["voc_desk_session"] = self.token
        self.assertEqual(csrf.post(self.path).status_code, 403)
        self.api.assert_not_called()
        self.flow["metadata"]["labels"]["voc.company"] = str(uuid4())
        self.assertEqual(self.client.post(self.path).status_code, 404)
        self.cursor.execute("UPDATE public.staff_user SET role='staff' WHERE staff_user_uuid=%s", (self.staff,))
        self.api.reset_mock()
        self.assertEqual(self.client.post(self.path).status_code, 403)
        self.api.assert_not_called()

    def test_recovery_never_stops_a_recreated_workflow(self):
        training.request_stop(self.actor,self.name)
        self.flow["metadata"]["uid"] = str(uuid4())
        self.flow["spec"].pop("shutdown")
        with self.assertRaises(DeskError):
            training.reconcile_stop_request(self.name,self.company)
        self.assertEqual(self.stops, 1)
