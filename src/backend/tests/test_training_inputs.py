import hashlib
import json
from io import BytesIO
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.conf import settings
from apps.preferences import training_inputs as inputs


class ArtifactSelectionTests(TestCase):
    def test_archives_missing_and_changed_evaluations_are_excluded(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        kinds = {str(t):t for t in (pa.string(),pa.timestamp("us",tz="UTC"),pa.int64(),pa.float64())}
        fields = json.loads((Path(settings.BASE_DIR).parent/"contracts/training-snapshot.json").read_text())
        schema=pa.schema([(name,kinds[kind]) for kind,names in fields.items() for name in names])
        out=BytesIO()
        row = {"domain_uuid":"commerce-id", "body":"다른 상품이 왔습니다.", "sentiment_label_uuid":"s",
               "sentiment_value":"부정", "category_label_uuid":"c", "category_uuid":"wrong-delivery"}
        pq.write_table(pa.Table.from_pylist([row], schema=schema),out)
        payload = out.getvalue()
        digest = hashlib.sha256(payload).hexdigest()
        uri = "s3://tests/manifests/frozen.parquet"
        s3 = Mock()
        s3.head_object.return_value = {"ContentLength": len(payload), "ETag": "v1"}
        cache.clear()
        domains = {"commerce-id":{"task":"commerce","categories":{"wrong-delivery":"wrong_delivery"}}}
        with patch.object(inputs,"storage", return_value=s3), patch.object(inputs,"training_domains",return_value=domains):
            self.assertFalse(inputs.evaluation_available("local-demo://archived/eval",digest,1))
            s3.head_object.assert_not_called()
            s3.get_object.return_value = {"Body": BytesIO(payload)}
            self.assertTrue(inputs.evaluation_available(uri,digest,1))
            self.assertTrue(inputs.evaluation_available(uri,digest,1))
            s3.get_object.assert_called_once()
            # A real, unchanged evaluation file can become unusable if the trainer's map changes.
            with patch.object(inputs,"training_domains",return_value={"other":domains["commerce-id"]}):
                s3.get_object.return_value = {"Body": BytesIO(payload)}
                self.assertFalse(inputs.evaluation_available(uri,digest,1))
            s3.get_object.return_value = {"Body": BytesIO(payload)}
            self.assertFalse(inputs.evaluation_available(uri,digest,2))
            s3.head_object.return_value["ETag"]="v2"
            s3.get_object.return_value = {"Body": BytesIO(payload+b"broken")}
            self.assertFalse(inputs.evaluation_available(uri,digest,1))
            s3.head_object.side_effect=OSError("unavailable")
            self.assertFalse(inputs.evaluation_available(uri,digest,1))
        for invalid in ({"sentiment_value":"unknown"}, {"category_uuid":"missing"}, {"unresolved_count":-1}):
            with self.subTest(invalid=invalid):
                invalid_payload = BytesIO()
                pq.write_table(pa.Table.from_pylist([row, {**row, **invalid}], schema=schema), invalid_payload)
                self.assertFalse(inputs.valid_snapshot(invalid_payload, 2, domains))
