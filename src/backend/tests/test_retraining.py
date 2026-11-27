import os
from datetime import datetime, time, timedelta
from io import StringIO
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.db import connection, transaction
from django.core.management import call_command
from django.test import override_settings

from apps.preferences import retraining as sched
from apps.preferences import training
from config.http import DeskError


class WindowTests(TestCase):
    def test_midnight_and_triggers(self):
        start, end = time(23), time(4)
        now = datetime(2026,1,12,0,0,tzinfo=sched.SEOUL)
        self.assertEqual(sched.quiet_window(now,start,end),now-timedelta(hours=1))
        self.assertIsNone(sched.quiet_window(now.replace(hour=4),start,end))
        self.assertEqual(sched.quiet_window(now.replace(hour=23),start,end),now.replace(hour=23))
        for mode,period_met,volume_met,expected in (
                ("period",True,False,True),("period",False,True,False),("volume",False,True,True),
                ("either",True,False,True),("both",False,True,False),("both",True,True,True)):
            policy={**sched.DEFAULTS,"trigger_mode":mode}
            cutoff=now-timedelta(days=7 if period_met else 6)
            self.assertEqual(sched.due(policy,now,cutoff,100 if volume_met else 1),expected)
            self.assertFalse(sched.due(policy,now,cutoff,0))


@skipUnless(os.environ.get("VOC_RETRAINING_TEST_DB")=="1","Use a local TiCats database with retraining schema")
class SchedulerTests(TestCase):
    """Database work is rolled back. Only the external registry and Argo API are stubbed."""
    def setUp(self):
        self.enterContext(override_settings(MLFLOW_TRACKING_URI="http://registry.test",ARGO_WORKFLOWS_URL="http://argo.test"))
        self.enterContext(transaction.atomic())
        self.addCleanup(transaction.set_rollback,True)
        self.company,self.staff,self.domain=uuid4(),uuid4(),uuid4()
        self.now=datetime(2026,1,12,3,tzinfo=sched.SEOUL)
        self.clock=self.enterContext(patch("django.utils.timezone.now",return_value=self.now))
        self.actor={"role":"super_admin","staff_user_uuid":self.staff,"company_uuid":self.company}
        self.model="models:/voc-bert-base/1"
        self.eval=str(uuid4())
        self.cursor=self.enterContext(connection.cursor())
        self.cursor.execute("INSERT INTO public.domain(domain_uuid,name) VALUES (%s,'시험')",(self.domain,))
        self.cursor.execute("INSERT INTO public.company(company_uuid,email_domain) VALUES (%s,'schedule.test')",(self.company,))
        self.cursor.execute("INSERT INTO public.staff_user(staff_user_uuid,company_uuid,email,password_hash,role) VALUES (%s,%s,'qa','!','super_admin')",(self.staff,self.company))
        self.cursor.execute("""INSERT INTO voc.training_dataset(company_uuid,dataset_version,purpose,selection_sha256,
            snapshot_ref,sha256,row_count,finalized_at) VALUES(%s,%s,'eval',%s,'s3://tests/frozen.parquet',repeat('a',64),1,now()) RETURNING dataset_uuid""",
            (self.company,self.eval,self.eval))
        self.dataset_uuid=self.cursor.fetchone()[0]
        self.options={"enabled":True,"revision":0,"trigger_mode":"volume","min_new_gold":2,"model":self.model,"eval_dataset":self.eval}
        self.flows={}
        self.submits=0
        self.lose_reply=False
        self.enterContext(patch.object(training,"available_models",return_value=[(self.model,"시작 모델")]))
        self.enterContext(patch("apps.preferences.training_inputs.evaluation_available",return_value=True))
        self.api=self.enterContext(patch.object(training,"argo_request",side_effect=self.argo))

    def argo(self,path="",payload=None,query=None,method=None):
        if path.endswith("/stop") and method == "PUT":
            flow = self.flows[path.split("/")[1]]
            flow["spec"]["shutdown"] = "Stop"
            return flow
        if path=="/submit":
            args=payload["submitOptions"]
            name=args["name"]
            self.submits+=1
            flow={"metadata":{"name":name,"uid":str(uuid4()),"labels":dict(item.split("=",1) for item in args["labels"].split(","))},
                  "spec":{"workflowTemplateRef":{"name":"voc-training"},
                          "arguments":{"parameters":[dict(zip(("name","value"),item.split("=",1))) for item in args["parameters"]]}},
                  "status":{"phase":"Running"}}
            self.flows[name]=flow
            if self.lose_reply:
                self.lose_reply=False
                raise OSError("response lost")
            return flow
        if not path:
            return {"items":list(self.flows.values())}
        if path[1:] not in self.flows:
            from urllib.error import HTTPError
            raise HTTPError("http://argo.test",404,"test",None,None)
        return self.flows[path[1:]]

    def gold(self,when,body):
        feature=uuid4()
        self.cursor.execute("""INSERT INTO voc.feature_snapshot(feature_uuid,company_uuid,domain_uuid,body,received_at,available_at,seed_ref)
            VALUES (%s,%s,%s,%s,%s,%s,%s)""",(feature,self.company,self.domain,body,when,when,str(feature)))
        self.cursor.execute("""INSERT INTO voc.gold_label(feature_uuid,label_kind,sentiment_value,label_origin,as_of_at,confirmed_at,evidence)
            VALUES(%s,'sentiment','중립','counselor',%s,%s,'{}') RETURNING gold_label_uuid""",(feature,when,when))
        return feature,self.cursor.fetchone()[0]

    def test_window_volume_retry_and_success_watermark(self):
        sched.save_policy(self.actor,self.options)
        day=self.now.replace(hour=11)
        self.gold(day,"새 문의 1")
        self.gold(day,"새 문의 2")
        self.assertEqual(sched.process_company(self.company,day),"waiting")
        self.assertFalse(self.api.called)
        night=self.now+timedelta(days=1)
        self.clock.return_value=night
        self.lose_reply=True
        self.assertEqual(sched.process_company(self.company,night),"unavailable")
        self.assertEqual(sched.process_company(self.company,night),"running")
        self.assertEqual(self.submits,1)
        flow=next(iter(self.flows.values()))
        self.assertEqual(flow["metadata"]["labels"]["voc.trigger"],"scheduled")
        flow["status"]["phase"]="Succeeded"
        self.assertEqual(sched.process_company(self.company,night),"succeeded")
        self.assertEqual(sched.read_policy(self.company,night)["new_gold"],0)
        self.assertEqual(sched.process_company(self.company,night),"waiting")

    def test_failed_run_preserves_data_and_busy_run_waits(self):
        sched.save_policy(self.actor,{**self.options,"min_new_gold":1})
        night=self.now+timedelta(minutes=5)
        self.gold(night,"검수 완료")
        self.clock.return_value=night
        selection = {"request_id": uuid4(), "as_of_at": self.now, "model": self.model, "eval_dataset": self.eval}
        manual = training.submit(self.actor, selection)
        self.assertEqual(sched.process_company(self.company, night), "busy")
        self.assertEqual(self.submits, 1)
        self.flows[manual]["status"]["phase"] = "Succeeded"
        self.assertEqual(sched.process_company(self.company,night),"submitted")
        scheduled = next(flow for name, flow in self.flows.items() if name != manual)
        scheduled["status"]["phase"] = "Failed"
        self.assertEqual(sched.process_company(self.company,night),"failed")
        self.assertEqual(sched.read_policy(self.company,night)["new_gold"],1)
        self.assertEqual(sched.process_company(self.company,night),"waiting")
        tomorrow=night+timedelta(days=1)
        self.clock.return_value=tomorrow
        self.assertEqual(sched.process_company(self.company,tomorrow),"submitted")
        self.assertEqual(self.submits,3)

    def test_heldout_filtered_and_disabled_policy_never_submits(self):
        sched.save_policy(self.actor,self.options)
        tick=self.now+timedelta(minutes=5)
        feature,label=self.gold(tick,"  같은  본문 ")
        self.cursor.execute("""INSERT INTO voc.training_dataset_item(dataset_uuid,feature_uuid,as_of_at,sentiment_label_uuid)
            VALUES (%s,%s,%s,%s)""",(self.dataset_uuid,feature,tick,label))
        self.gold(tick,"같은 본문")
        self.gold(tick,"신규 상담")
        self.assertEqual(sched.read_policy(self.company,tick)["new_gold"],1)
        with self.assertRaises(sched.PolicyConflict):
            sched.save_policy(self.actor,self.options)
        with self.assertRaises(DeskError):
            sched.save_policy({**self.actor,"role":"staff"},self.options)
        sched.save_policy(self.actor,{"revision":1,"enabled":False})
        self.assertEqual(sched.process_company(self.company,tick),"waiting")
        self.assertFalse(self.api.called)

    def test_disconnected_policy_waits_and_can_be_disabled(self):
        sched.save_policy(self.actor, {**self.options, "min_new_gold": 1})
        night = self.now + timedelta(minutes=5)
        self.gold(night, "연결이 복구되기 전에 검수한 상담")
        self.clock.return_value = night
        with override_settings(ARGO_WORKFLOWS_URL=""):
            self.assertEqual(sched.process_company(self.company, night), "unavailable")
            self.assertEqual(sched.read_policy(self.company, night)["status"], "Argo 연결 대기")
            self.assertFalse(self.api.called)
        with override_settings(MLFLOW_TRACKING_URI=""):
            self.assertEqual(sched.process_company(self.company, night), "unavailable")
            self.assertIsNone(sched.read_policy(self.company, night)["last_workflow"])
        sched.save_policy(self.actor, {"revision": 1, "enabled": False})
        self.assertEqual(sched.process_company(self.company, night), "cancelled")
        self.assertEqual(self.submits, 0)

    def test_cannot_enable_using_another_companys_evaluation_set(self):
        other_company, other_admin = uuid4(), uuid4()
        self.cursor.execute("INSERT INTO public.company(company_uuid,email_domain) VALUES (%s,'other.test')",
                            (other_company,))
        self.cursor.execute("""INSERT INTO public.staff_user(staff_user_uuid,company_uuid,email,password_hash,role)
            VALUES (%s,%s,'qa','!','super_admin')""", (other_admin, other_company))
        actor = {**self.actor, "company_uuid": other_company, "staff_user_uuid": other_admin}
        with self.assertRaisesRegex(ValueError, "고정 평가셋"):
            sched.save_policy(actor, self.options)
        self.assertEqual(sched.read_policy(other_company)["revision"], 0)

    def test_disabled_draft_preserves_manual_submission_without_listing(self):
        sched.save_policy(self.actor, {"revision": 0, "enabled": False})
        training.submit(self.actor, {"request_id": uuid4(), "as_of_at": self.now,
                                     "model": self.model, "eval_dataset": self.eval})
        self.assertEqual([call.args[0] for call in self.api.call_args_list], ["/submit"])

    def test_no_submission_if_slot_check_crosses_window_end(self):
        deadline = self.now + timedelta(seconds=2)
        self.clock.return_value = deadline
        with self.assertRaises(training.TrainingBusy):
            training.submit({**self.actor, "scheduled": True, "submit_before": deadline},
                            {"request_id": uuid4(), "as_of_at": self.now,
                             "model": self.model, "eval_dataset": self.eval})
        self.assertEqual(self.submits, 0)

    def insert_stop_intent(self, name, requested_at, cutoff):
        self.cursor.execute("""INSERT INTO voc.training_stop_request
            (workflow_name,workflow_uid,company_uuid,requested_by,requested_at,cutoff)
            VALUES (%s,%s,%s,%s,%s,%s)""",
            (name, self.flows[name]["metadata"]["uid"], self.company, self.staff, requested_at, cutoff))

    def test_user_stop_requires_new_gold_and_preserves_manual_training(self):
        sched.save_policy(self.actor, self.options)
        cutoff = self.now + timedelta(minutes=1)
        self.clock.return_value = cutoff
        self.gold(cutoff, "중지 전 첫 표본")
        self.gold(cutoff, "중지 전 둘째 표본")
        self.assertEqual(sched.process_company(self.company, cutoff), "submitted")
        name, flow = next(iter(self.flows.items()))
        requested_at = cutoff + timedelta(minutes=1)
        self.clock.return_value = requested_at
        self.insert_stop_intent(name, requested_at, cutoff)
        self.assertEqual(sched.process_company(self.company, requested_at), "running")
        self.assertEqual(flow["spec"]["shutdown"], "Stop")
        self.assertEqual(sched.read_policy(self.company, requested_at)["status"], "중지 요청 중")
        self.assertEqual(self.submits, 1)
        flow["status"]["phase"] = "Failed"
        self.assertEqual(sched.process_company(self.company, requested_at), "stopped")
        policy = sched.read_policy(self.company, requested_at)
        self.assertTrue(policy["enabled"])
        self.assertEqual(policy["revision"], 1)
        self.assertEqual(policy["last_cutoff"], requested_at)
        self.assertEqual(policy["new_gold"], 0)
        tomorrow = requested_at + timedelta(days=1)
        self.clock.return_value = tomorrow
        self.assertEqual(sched.process_company(self.company, tomorrow), "waiting")
        # A manual run can explicitly use the original data cutoff.
        manual = training.submit(self.actor, {"request_id": uuid4(), "as_of_at": cutoff,
                                "model": self.model, "eval_dataset": self.eval})
        self.flows[manual]["status"]["phase"] = "Succeeded"
        self.gold(tomorrow, "중지 후 첫 표본")
        self.assertEqual(sched.process_company(self.company, tomorrow), "waiting")
        self.gold(tomorrow, "중지 후 둘째 표본")
        self.assertEqual(sched.process_company(self.company, tomorrow), "submitted")
        self.assertEqual(self.submits, 3)
        self.cursor.execute("""SELECT count(*) FROM voc.gold_label g JOIN voc.feature_snapshot f USING(feature_uuid)
            WHERE f.company_uuid=%s""", (self.company,))
        self.assertEqual(self.cursor.fetchone()[0], 4)

    def test_command_recovers_manual_stop_without_policy(self):
        name = training.submit(self.actor, {"request_id": uuid4(), "as_of_at": self.now,
                               "model": self.model, "eval_dataset": self.eval})
        self.insert_stop_intent(name, self.now, self.now)
        # Keep this test's enclosing rollback transaction on its single connection.
        self.enterContext(patch("apps.preferences.management.commands.run_retraining_scheduler.close_old_connections"))
        call_command("run_retraining_scheduler", once=True, company=self.company, stdout=StringIO())
        self.assertEqual(self.flows[name]["spec"]["shutdown"], "Stop")
        self.flows[name]["status"]["phase"] = "Failed"
        call_command("run_retraining_scheduler", once=True, company=self.company, stdout=StringIO())
        self.cursor.execute("SELECT state,confirmed_at FROM voc.training_stop_request WHERE workflow_name=%s", (name,))
        state, confirmed = self.cursor.fetchone()
        self.assertEqual(state, "stopped")
        self.assertIsNotNone(confirmed)
        self.assertEqual(self.submits, 1)

    def test_natural_completion_uses_training_cutoff_and_keeps_newer_reviews(self):
        sched.save_policy(self.actor, {**self.options, "min_new_gold": 1})
        cutoff = self.now + timedelta(minutes=1)
        self.clock.return_value = cutoff
        self.gold(cutoff, "이번 학습 표본")
        self.assertEqual(sched.process_company(self.company, cutoff), "submitted")
        name, flow = next(iter(self.flows.items()))
        requested_at = cutoff + timedelta(minutes=5)
        self.clock.return_value = requested_at
        self.gold(cutoff + timedelta(minutes=3), "진행 중 접수")
        self.insert_stop_intent(name, requested_at, cutoff)
        flow["status"]["phase"] = "Succeeded"
        self.assertEqual(sched.process_company(self.company, requested_at), "succeeded")
        self.assertEqual(sched.read_policy(self.company, requested_at)["new_gold"], 1)

    def test_period_restarts_from_stop_request_and_needs_new_gold(self):
        sched.save_policy(self.actor, {**self.options, "trigger_mode": "period", "interval_days": 1})
        cutoff = self.now + timedelta(days=1)
        self.clock.return_value = cutoff
        self.gold(cutoff, "첫 주기")
        self.assertEqual(sched.process_company(self.company, cutoff), "submitted")
        name, flow = next(iter(self.flows.items()))
        requested_at = cutoff + timedelta(minutes=8)
        self.insert_stop_intent(name, requested_at, cutoff)
        flow["spec"]["shutdown"] = "Stop"
        flow["status"]["phase"] = "Failed"
        self.assertEqual(sched.process_company(self.company, requested_at), "stopped")
        tomorrow = cutoff + timedelta(days=1)
        self.gold(tomorrow, "다음 주기")
        self.clock.return_value = tomorrow
        self.assertEqual(sched.process_company(self.company, tomorrow), "waiting")
        self.clock.return_value = tomorrow + timedelta(minutes=8)
        self.assertEqual(sched.process_company(self.company, self.clock.return_value), "submitted")
        self.assertEqual(self.submits, 2)

    def test_workflow_missing_after_stop_intent_never_resubmits_its_name(self):
        sched.save_policy(self.actor, {**self.options, "min_new_gold": 1})
        cutoff = self.now + timedelta(minutes=5)
        self.clock.return_value = cutoff
        self.gold(cutoff, "기록 보관 종료 시연")
        self.assertEqual(sched.process_company(self.company, cutoff), "submitted")
        name = next(iter(self.flows))
        self.insert_stop_intent(name, cutoff, cutoff)
        del self.flows[name]
        self.assertEqual(sched.process_company(self.company, cutoff), "cancelled")
        self.assertEqual(sched.process_company(self.company, cutoff + timedelta(days=1)), "waiting")
        self.cursor.execute("SELECT state FROM voc.training_stop_request WHERE workflow_name=%s", (name,))
        self.assertEqual(self.cursor.fetchone()[0], "requested")
        self.assertEqual(self.submits, 1)
