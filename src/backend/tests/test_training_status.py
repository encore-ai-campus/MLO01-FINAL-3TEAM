from unittest import TestCase

from apps.preferences.training_status import project


class TrainingStatusTests(TestCase):
    def test_pending_and_completed_old_run(self):
        pending = project({"status": {"phase": "Pending"}})
        self.assertEqual([point["state"] for point in pending["overall"]], ["done", "active", "waiting", "waiting"])
        done = project({"status": {"phase": "Succeeded"}})
        self.assertTrue(done["terminal"])
        self.assertTrue(all(p["state"] == "done" for p in done["overall"]))
        self.assertEqual(done["cards"][0]["rows"][0]["message"], "세부 기록 없음")

    def test_current_retry_ignores_failed_attempt_and_stale_records(self):
        nodes = {
            "parent": {"type": "Retry", "phase": "Running", "children": ["old", "current"]},
            "old": {"id": "old", "type": "Pod", "phase": "Failed", "templateName": "train"},
            "current": {"id": "current", "type": "Pod", "phase": "Running", "templateName": "train",
                        "startedAt": "2026-11-27T00:00:02Z"},
        }
        records = [
            {"node_id": "old", "operation": "training", "state": "failed", "updated_at": "2026-11-27T00:00:01Z"},
            {"node_id": "current", "operation": "model_load", "state": "completed", "updated_at": "2026-11-27T00:00:01Z"},
            {"node_id": "current", "operation": "training", "state": "running", "updated_at": "2026-11-27T00:00:03Z"},
        ]
        result = project({"status": {"phase": "Running", "nodes": nodes}}, records)
        self.assertEqual(result["label"], "재시도")
        self.assertEqual(result["cards"][1]["rows"][0]["points"][0]["state"], "waiting")
        self.assertIn("재시도 중", result["cards"][1]["rows"][1]["message"])

    def test_argo_failure_marks_the_unfinished_operation(self):
        workflow = {"status": {"phase": "Failed", "nodes": {
            "pod": {"id": "pod", "type": "Pod", "templateName": "prepare", "phase": "Failed",
                    "message": "OOMKilled"},
        }}}
        records = [{"node_id": "pod", "operation": "feast", "state": "running",
                    "updated_at": "2026-11-27T00:00:01Z"}]
        result = project(workflow, records)
        self.assertEqual(result["overall"][2]["state"], "failed")
        self.assertIn("메모리", result["cards"][0]["rows"][2]["message"])

    def test_registry_completion_waits_for_workflow_success(self):
        workflow = {"status": {"phase": "Running", "nodes": {
            "pod": {"id": "pod", "type": "Pod", "templateName": "register", "phase": "Succeeded"},
        }}}
        records = [{"node_id": "pod", "operation": "registry", "state": "completed",
                    "updated_at": "2026-11-27T00:00:01Z"}]
        running = project(workflow, records)
        self.assertEqual(running["cards"][1]["rows"][-1]["points"][-1]["state"], "done")
        self.assertEqual(running["overall"][-1]["state"], "waiting")
        self.assertFalse(running["terminal"])
        workflow["status"]["phase"] = "Succeeded"
        self.assertTrue(project(workflow, records)["terminal"])

    def test_reused_dataset_preserves_verified_path(self):
        workflow = {"status": {"phase": "Running", "nodes": {
            "pod": {"id": "pod", "type": "Pod", "templateName": "prepare", "phase": "Succeeded"},
        }}}
        path = "s3://training/snapshot"
        records = [{"node_id": "pod", "operation": "dataset_save", "state": "reused",
                    "updated_at": "2026-11-27T00:00:01Z", "paths": {"train": path}}]
        result = project(workflow, records)
        self.assertEqual(result["cards"][0]["rows"][3]["message"], "재사용")
        self.assertEqual(result["paths"]["train"], path)
