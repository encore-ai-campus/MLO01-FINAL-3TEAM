from unittest import TestCase
from unittest.mock import Mock, patch

import psycopg

from .progress import Progress


class ProgressTests(TestCase):
    def test_stage_records_transitions_and_reuse(self):
        recorder = Progress()
        with patch.object(recorder, "write") as write:
            with recorder.stage("dataset_save") as result:
                result["state"] = "reused"
                result["paths"] = {"train": "s3://data/snapshot"}
        self.assertEqual([item.args[1:] or (item.kwargs["state"],) for item in write.call_args_list],
                         [("requested",), ("running",), ("reused",)])
        self.assertEqual(write.call_args.kwargs["paths"], {"train": "s3://data/snapshot"})

    def test_failure_records_a_safe_reason_and_reraises(self):
        recorder = Progress()
        with patch.object(recorder, "write") as write, self.assertRaises(ValueError):
            with recorder.stage("feast"):
                raise ValueError("raw exception details")
        write.assert_called_with("feast", "failed", reason="invalid_data")

    @patch.dict("os.environ", {"DATABASE_URL": "dbname=unused"})
    @patch("ml.training.progress.psycopg.connect")
    def test_writes_are_independent_and_scoped_to_the_node(self, connect):
        with Progress("workflow", "node-2") as recorder:
            recorder.write("feast", "running")
        self.assertTrue(connect.call_args.kwargs["autocommit"])
        self.assertEqual(connect.return_value.execute.call_args.args[1][:4],
                         ("workflow", "node-2", "feast", "running"))
        connect.return_value.close.assert_called_once()

    @patch.dict("os.environ", {"DATABASE_URL": "dbname=unused"})
    @patch("ml.training.progress.psycopg.connect", side_effect=psycopg.OperationalError)
    def test_recording_outage_does_not_interrupt_the_stage(self, connect):
        operation = Mock()
        with self.assertLogs("ml.training.progress"), Progress("workflow", "node-2") as recorder:
            with recorder.stage("postgres"):
                operation()
        operation.assert_called_once()
        connect.assert_called_once()
