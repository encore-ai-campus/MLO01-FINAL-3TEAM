import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, sleep
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from server import create_app
from voc_models.tickets import read_domain_map

DOMAIN_ID = "f7adfece-886c-4e9d-8aa6-427a875bbf69"
REQUEST = {
    "request_id": "62A02C0C-9F13-4610-A99E-7A665AD5B02F",
    "analysis_kind": "category",
    "domain_id": DOMAIN_ID,
    "model_text": "주문한 상품이 사흘째 도착하지 않았어요.",
}


class ModelServerTests(unittest.TestCase):
    def wait_ready(self, client):
        deadline = monotonic() + 3
        while monotonic() < deadline:
            if client.get("/readyz").status_code == 200:
                return
            sleep(0.01)
        self.fail("The model did not become ready")

    def test_stable_returns_one_label_and_preserves_request_identity(self):
        def predictor(kind, domain_id, body):
            self.assertEqual(domain_id, DOMAIN_ID)
            self.assertEqual(body, REQUEST["model_text"])
            return {"label": "delivery", "score": 0.94} if kind == "category" else {"label": "부정"}

        with (
            patch.dict(os.environ, {"MODEL_VERSION": "voc-test-v1", "MODEL_ANALYSIS_KINDS": "sentiment,category"}),
            patch("server._load_predictor", return_value=predictor) as load,
            TestClient(create_app()) as client,
        ):
            self.wait_ready(client)
            first = client.post("/v1/predict", json=REQUEST)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json(), {
                "request_id": REQUEST["request_id"],
                "analysis_kind": "category",
                "model_version": "voc-test-v1",
                "label": "delivery",
                "score": 0.94,
            })
            self.assertEqual(client.post("/v1/predict", json=REQUEST).json(), first.json())
            sentiment = client.post("/v1/predict", json={**REQUEST, "analysis_kind": "sentiment"})
            self.assertEqual(sentiment.status_code, 200)
            self.assertEqual(sentiment.json()["label"], "부정")
            self.assertNotIn("score", sentiment.json())
            self.assertIn('analysis_kind="category",outcome="success"', client.get("/metrics").text)
            load.assert_called_once()

    def test_health_loading_and_category_hold(self):
        started, available = Event(), Event()

        def load():
            started.set()
            available.wait(3)
            return lambda *_args: {"label": None}

        with (
            patch.dict(os.environ, {"MODEL_VERSION": "voc-test-v1", "MODEL_ANALYSIS_KINDS": "sentiment,category"}),
            patch("server._load_predictor", side_effect=load),
            TestClient(create_app()) as client,
        ):
            try:
                self.assertTrue(started.wait(3))
                self.assertEqual(client.get("/livez").status_code, 200)
                self.assertEqual(client.get("/readyz").status_code, 503)
                self.assertEqual(client.post("/v1/predict", json=REQUEST).status_code, 503)
            finally:
                available.set()
            self.wait_ready(client)
            response = client.post("/v1/predict", json=REQUEST)
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.json()["label"])
            self.assertNotIn("score", response.json())

    def test_canary_only_accepts_sentiment_and_validates_input(self):
        with (
            patch.dict(os.environ, {"MODEL_VERSION": "voc-canary-v2", "MODEL_ANALYSIS_KINDS": "sentiment"}),
            patch("server._load_predictor", return_value=lambda *_args: {"label": "매우 부정"}),
            TestClient(create_app()) as client,
        ):
            self.wait_ready(client)
            self.assertEqual(client.post("/v1/predict", json=REQUEST).status_code, 422)
            self.assertEqual(client.post("/v1/predict", json={**REQUEST, "model_text": ""}).status_code, 422)
            response = client.post("/v1/predict", json={**REQUEST, "analysis_kind": "sentiment"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["model_version"], "voc-canary-v2")

    def test_bad_backend_score_returns_generic_error(self):
        with (
            patch.dict(os.environ, {"MODEL_VERSION": "voc-test-v1", "MODEL_ANALYSIS_KINDS": "category"}),
            patch("server._load_predictor", return_value=lambda *_args: {"label": "delivery", "score": float("nan")}),
            TestClient(create_app()) as client,
        ):
            self.wait_ready(client)
            response = client.post("/v1/predict", json=REQUEST)
            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.json(), {"detail": "Prediction failed"})

    def test_domain_map_requires_an_explicit_code_or_hold_for_each_label(self):
        task_labels = {"commerce": {"delivery_delay": 0, "wrong_delivery": 1}}
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "domains.json"
            path.write_text(json.dumps({
                DOMAIN_ID: {
                    "task": "commerce",
                    "labels": {"delivery_delay": "delivery", "wrong_delivery": None},
                },
            }), encoding="utf-8")
            self.assertEqual(read_domain_map(path, task_labels)[DOMAIN_ID], (
                "commerce", {"delivery_delay": "delivery", "wrong_delivery": None}, 0.0
            ))
            with self.assertRaises(ValueError):
                read_domain_map(path, {"commerce": {"delivery_delay": 0, "other": 1}})


if __name__ == "__main__":
    unittest.main()

