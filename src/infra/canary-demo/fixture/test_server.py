import os
import sys
from types import ModuleType
from unittest import TestCase
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from server import Prediction, PredictionRequest, create_app


REQUEST = {
    "request_id": "62a02c0c-9f13-4610-a99e-7a665ad5b02f",
    "domain_id": "f7adfece-886c-4e9d-8aa6-427a875bbf69",
    "model_text": "  배송에 관해 문의합니다.\n",
    "analysis_kind": "sentiment",
}


class ServingTests(TestCase):
    def client(self, version, backend="stub"):
        with patch.dict(os.environ, {"MODEL_VERSION": version, "MODEL_BACKEND": backend}):
            client = TestClient(create_app())
        self.addCleanup(client.close)
        return client

    def test_each_release_echoes_identity_for_both_tasks(self):
        for version in ("stub-v1", "stub-v2"):
            client = self.client(version)
            self.assertEqual(client.get("/readyz").json()["model_version"], version)
            self.assertTrue(client.get("/livez").json()["live"])
            baseline = client.get("/metrics").text
            self.assertIn(f'voc_inference_requests_total{{analysis_kind="sentiment",model_version="{version}",outcome="error"}} 0.0', baseline)
            for kind, label in (("category", "delivery"), ("sentiment", "중립")):
                response = client.post("/v1/predict", json={**REQUEST, "analysis_kind": kind})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {
                    "request_id": REQUEST["request_id"], "analysis_kind": kind,
                    "model_version": version, "label": label,
                })
            metrics = client.get("/metrics").text
            self.assertIn('voc_inference_requests_total{analysis_kind="sentiment",model_version="'+version+'",outcome="success"} 1.0', metrics)
            self.assertIn("voc_inference_duration_seconds_bucket{", metrics)
            for private_value in (REQUEST["request_id"], REQUEST["domain_id"], REQUEST["model_text"].strip()):
                self.assertNotIn(private_value, metrics)

    def test_bad_request_is_not_echoed(self):
        client = self.client("stub-v1")
        for changes in ({"analysis_kind": "unsupported"}, {"model_text": " "},
                        {"domain_id": "not-uuid"}, {"request_id": ""}, {"raw_payload": REQUEST["model_text"]}):
            response = client.post("/v1/predict", json={**REQUEST, **changes})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json(), {"error": {"code": "invalid_request"}})
        self.assertIn('outcome="invalid"} 5.0', client.get("/metrics").text)

    def test_preserves_text_and_supports_category_abstention(self):
        self.assertEqual(PredictionRequest(**REQUEST).model_text, REQUEST["model_text"])
        fields = {"request_id": REQUEST["request_id"], "model_version": "real-v1", "label": None}
        self.assertIsNone(Prediction(**fields, analysis_kind="category").label)
        with self.assertRaises(ValidationError):
            Prediction(**fields, analysis_kind="sentiment")

    def test_fixture_refuses_to_claim_an_actual_model_version(self):
        with patch.dict(os.environ, {"MODEL_VERSION": "production-v1"}, clear=True), self.assertRaises(RuntimeError):
            create_app()

    def model_client(self, loader):
        module = ModuleType("voc_models.test_adapter")
        module.load = loader
        with patch.dict(sys.modules, {module.__name__: module}):
            return self.client("candidate-v3", module.__name__ + ":load")

    def test_loads_adapter_once_and_preserves_features(self):
        predictor = Mock(side_effect=[{"label": "긍정", "score": 0.87}, {"label": None}])
        loader = Mock(return_value=predictor)
        client = self.model_client(loader)
        self.assertEqual(client.get("/readyz").json(), {
            "ready": True, "implementation": "python", "model_version": "candidate-v3"})
        for kind, expected in (("sentiment", {"label": "긍정", "score": 0.87}), ("category", {"label": None})):
            response = client.post("/v1/predict", json={**REQUEST, "analysis_kind": kind})
            predictor.assert_called_with(kind, REQUEST["domain_id"], REQUEST["model_text"])
            self.assertEqual(response.json(), {
                "request_id": REQUEST["request_id"], "analysis_kind": kind, "model_version": "candidate-v3", **expected})
        loader.assert_called_once_with()
        self.assertEqual(client.post("/v1/predict", json={**REQUEST, "MODEL_BACKEND": "other:load"}).status_code, 422)
        self.assertEqual(predictor.call_count, 2)

    def test_adapter_failure_does_not_echo_input(self):
        private = REQUEST["model_text"]
        predictor = Mock(side_effect=[ValueError(private), {"label": private}, {"label": "중립", "score": 7},
                                      {"label": "중립", "request_id": "replaced"}])
        client = self.model_client(lambda: predictor)
        for _ in range(4):
            response = client.post("/v1/predict", json=REQUEST)
            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.json(), {"error": {"code": "inference_failed"}})
        metrics = client.get("/metrics").text
        self.assertIn('outcome="error"} 4.0', metrics)
        self.assertNotIn(private.strip(), metrics)

    def test_load_failure_keeps_pod_out_of_service(self):
        with self.assertLogs("server", level="ERROR") as logs:
            client = self.model_client(Mock(side_effect=ValueError(REQUEST["model_text"])))
        self.assertNotIn(REQUEST["model_text"].strip(), str(logs.output))
        readiness = client.get("/readyz")
        self.assertEqual(readiness.status_code, 503)
        self.assertFalse(readiness.json()["ready"])
        self.assertEqual(client.get("/livez").status_code, 200)
        response = client.post("/v1/predict", json=REQUEST)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": {"code": "model_unavailable"}})

        async def unsupported(*args):
            return {"label": "중립"}

        for invalid_loader in (unsupported, lambda: unsupported, lambda: None):
            with self.assertLogs("server", level="ERROR"):
                self.assertEqual(self.model_client(invalid_loader).get("/readyz").status_code, 503)

    def test_backend_and_version_are_deployment_configuration(self):
        for backend, version in (("os:system", "v1"), ("voc_models.foo:__getattr__", "v1"),
                                 ("voc_models.foo:load", ""), ("voc_models.foo:load", "stub-v1")):
            with self.subTest(backend=backend, version=version), self.assertRaises(RuntimeError):
                self.client(version, backend)
