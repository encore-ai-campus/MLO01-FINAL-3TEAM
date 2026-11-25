"""Versioned inference HTTP interface for the local fixture and model images."""

from importlib import import_module
from inspect import iscoroutinefunction
import logging
import os
import re
from time import monotonic
from typing import Literal
from uuid import UUID

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


AnalysisKind = Literal["sentiment", "category"]
SENTIMENT_LABELS = {"긍정", "중립", "부정", "매우 부정"}
FIXTURE_VERSIONS = {"stub-v1", "stub-v2"}
BACKEND_PATH = re.compile(r"voc_models(?:\.[a-zA-Z][a-zA-Z0-9_]*)+:[a-zA-Z][a-zA-Z0-9_]*")


class PredictionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128)
    analysis_kind: AnalysisKind
    model_text: str = Field(min_length=1)
    domain_id: UUID

    @field_validator("model_text", "request_id")
    @classmethod
    def require_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("nonblank input required")
        return value  # Preserve the exact versioned feature text.


class Prediction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    analysis_kind: AnalysisKind
    model_version: str
    label: str | None = Field(max_length=128)
    score: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False, strict=True)

    @model_validator(mode="after")
    def check_label(self):
        if self.analysis_kind == "sentiment" and self.label not in SENTIMENT_LABELS:
            raise ValueError("unexpected sentiment label")
        if self.analysis_kind == "category" and self.label is not None and not self.label.strip():
            raise ValueError("return a category code or null for abstention")
        return self


def fixture_predict(analysis_kind, domain_id, model_text):
    return {"label": "중립" if analysis_kind == "sentiment" else "delivery"}


def load_backend(version):
    backend = os.environ.get("MODEL_BACKEND", "stub")
    if backend == "stub":
        if version not in FIXTURE_VERSIONS:
            raise RuntimeError("Set MODEL_VERSION=stub-v1 or stub-v2 for the routing fixture")
        return "stub", fixture_predict
    if not BACKEND_PATH.fullmatch(backend):
        raise RuntimeError("MODEL_BACKEND must be voc_models.<module>:<loader>")
    if version in FIXTURE_VERSIONS or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,30}[a-z0-9])?", version):
        raise RuntimeError("Set a distinct model release ID matching the Helm version format")
    module, loader = backend.split(":")
    try:
        load = getattr(import_module(module), loader)
        if iscoroutinefunction(load):
            raise TypeError("expected a synchronous loader")
        predictor = load()
        if not callable(predictor) or iscoroutinefunction(predictor) or iscoroutinefunction(predictor.__call__):
            raise TypeError("expected a synchronous predictor")
    except Exception as error:
        logging.getLogger(__name__).error("Model backend startup failed (%s).", type(error).__name__)
        predictor = None
    return "python", predictor


def create_app() -> FastAPI:
    version = os.environ.get("MODEL_VERSION", "")
    implementation, predictor = load_backend(version)

    app = FastAPI(title="VoC classification", version="1")
    registry = CollectorRegistry()
    requests = Counter(
        "voc_inference_requests", "Completed inference HTTP requests",
        ("model_version", "analysis_kind", "outcome"), registry=registry,
    )
    durations = Histogram(
        "voc_inference_duration_seconds", "Time spent serving an inference HTTP request",
        ("model_version", "analysis_kind"), registry=registry,
    )
    # Publish the zero baseline so the first failure or short canary burst can be observed.
    for kind in ("sentiment", "category"):
        requests.labels(version, kind, "success")
        requests.labels(version, kind, "error")
        durations.labels(version, kind)
    requests.labels(version, "unknown", "invalid")

    @app.middleware("http")
    async def measure_prediction(request: Request, call_next):
        if request.method != "POST" or request.url.path != "/v1/predict":
            return await call_next(request)
        started = monotonic()
        outcome = "error"
        try:
            response = await call_next(request)
            if response.status_code == 200:
                outcome = "success"
            elif 400 <= response.status_code < 500:
                outcome = "invalid"
            return response
        finally:
            kind = getattr(request.state, "analysis_kind", "unknown")
            requests.labels(version, kind, outcome).inc()
            durations.labels(version, kind).observe(monotonic() - started)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, error: RequestValidationError):
        # FastAPI's default validation body can echo an invalid customer payload.
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_request"}})

    @app.get("/livez")
    def live():
        return {"live": True}

    @app.get("/readyz")
    def ready():
        return JSONResponse(status_code=200 if predictor is not None else 503, content={
            "ready": predictor is not None, "implementation": implementation, "model_version": version,
        })

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return Response(content=generate_latest(registry), headers={"Content-Type": CONTENT_TYPE_LATEST})

    @app.post("/v1/predict", response_model=Prediction, response_model_exclude_unset=True)
    def predict(data: PredictionRequest, request: Request) -> Prediction | JSONResponse:
        request.state.analysis_kind = data.analysis_kind
        if predictor is None:
            return JSONResponse(status_code=503, content={"error": {"code": "model_unavailable"}})
        try:
            output = predictor(data.analysis_kind, str(data.domain_id), data.model_text)
            return Prediction(request_id=data.request_id, analysis_kind=data.analysis_kind,
                              model_version=version, **output)
        except Exception:
            # Model exceptions and output validation errors may contain the input text.
            return JSONResponse(status_code=500, content={"error": {"code": "inference_failed"}})

    return app
