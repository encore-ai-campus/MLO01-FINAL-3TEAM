import io
from unittest import TestCase
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from .datasets import SCHEMA, checksum, exclude_evaluation, load_snapshot


class DatasetSafetyTests(TestCase):
    def test_same_conversation_or_duplicate_text_is_excluded_from_training(self):
        evaluation = [{"source_conversation": "thread-1", "text_sha256": "text-1"}]
        same_thread = {"source_conversation": "thread-1", "text_sha256": "rewritten"}
        copied_text = {"source_conversation": "thread-2", "text_sha256": "text-1"}
        independent = {"source_conversation": "thread-3", "text_sha256": "text-3"}
        self.assertEqual(exclude_evaluation([same_thread, copied_text, independent], evaluation), [independent])

    @patch("ml.training.datasets.s3")
    @patch("ml.training.datasets.read_object")
    def test_snapshot_accepts_expected_file_and_rejects_corruption_or_wrong_count(self, read, client):
        output = io.BytesIO()
        table = pa.Table.from_pylist([{"feature_uuid": "example"}], schema=SCHEMA)
        pq.write_table(table, output)
        contents = output.getvalue()
        dataset = {"snapshot_ref": "s3://training/data.parquet", "sha256": checksum(contents), "row_count": 1}
        read.return_value = contents
        self.assertEqual(load_snapshot(dataset), table.to_pylist())
        read.assert_called_with(client.return_value, {"Bucket": "training", "Key": "data.parquet"})
        with self.assertRaisesRegex(ValueError, "schema or row count mismatch"):
            load_snapshot({**dataset, "row_count": 2})
        read.return_value = contents + b"extra bytes"
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            load_snapshot(dataset)
