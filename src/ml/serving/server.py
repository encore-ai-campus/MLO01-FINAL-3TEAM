import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from importlib import import_module
import logging
import os
from time import perf_counter
from typing import Literal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, ConfigDict, field_validator

LOGGER = logging.getLogger(__name__)
KINDS = {"sentiment", "category"}
SENTIMENT_LABELS = {"긍정", "중립", "부정", "매우 부정"}


class PredictRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    request_id: str
    analysis_kind: Literal["sentiment", "category"]
    domain_id: str
    model_text: str

    @field_validator("request_id", "domain_id")
    @classmethod
    def validate_uuid(cls, value: str) -> str:
        UUID(value)
        return value

    @field_validator("model_text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model_text must contain a non-whitespace character")
        return value


class PredictResponse(BaseModel):
    request_id: str
    analysis_kind: Literal["sentiment", "category"]
    model_version: str
    label: str | None
    score: float | None = None


def _load_predictor():
    value = os.environ.get("MODEL_BACKEND", "voc_models.tickets:load")
    module_name, separator, attribute = value.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("MODEL_BACKEND must have the form module:loader")
    predict = getattr(import_module(module_name), attribute)()
    if not callable(predict):
        raise TypeError("The backend loader must return a predictor")
    return predict


def _validated_prediction(kind, result):
    if not isinstance(result, Mapping) or "label" not in result:
        raise TypeError("The predictor must return a mapping containing label")
    label = result["label"]
    if kind == "sentiment":
        if not isinstance(label, str) or label not in SENTIMENT_LABELS:
            raise ValueError("Invalid sentiment label")
    elif label is not None and (not isinstance(label, str) or not label.strip()):
        raise ValueError("Invalid category label")

    prediction = {"label": label}
    score = result.get("score")
    if score is not None:
        if type(score) not in (int, float) or not 0 <= score <= 1:
            raise ValueError("Prediction score must be a finite number from 0 to 1")
        prediction["score"] = score
    return prediction


def create_app() -> FastAPI:
    registry = CollectorRegistry()
    ready = Gauge("voc_model_ready", "Whether model loading completed", registry=registry)
    load_duration = Gauge("voc_model_load_seconds", "Last model load duration", registry=registry)
    count = Counter(
        "voc_model_predictions_total", "Prediction outcomes", ("analysis_kind", "outcome"), registry=registry
    )
    latency = Histogram(
        "voc_model_prediction_seconds", "Predictor execution duration in seconds",
        ("analysis_kind",), registry=registry,
    )

    async def initialize(app):
        started = perf_counter()
        try:
            version = os.environ.get("MODEL_VERSION", "")
            if not version.strip():
                raise ValueError("MODEL_VERSION must be set")
            kinds = {part.strip() for part in os.environ.get("MODEL_ANALYSIS_KINDS", "sentiment,category").split(",")}
            if not kinds or not kinds <= KINDS:
                raise ValueError("MODEL_ANALYSIS_KINDS must contain sentiment and/or category")
            predict = await asyncio.to_thread(_load_predictor)
        except Exception:
            LOGGER.exception("Unable to load the VoC model")
        else:
            app.state.model_version = version
            app.state.kinds = kinds
            app.state.predict = predict
            ready.set(1)
        finally:
            load_duration.set(perf_counter() - started)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.predict = None
        ready.set(0)
        task = asyncio.create_task(initialize(app))
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            app.state.predict = None
            ready.set(0)

    app = FastAPI(title="VoC Model API", lifespan=lifespan)
    app.state.predict = None

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exception: RequestValidationError):
        return JSONResponse({"detail": "Invalid prediction request"}, status_code=422)

    @app.get("/livez")
    def live():
        return {"status": "alive"}

    @app.get("/readyz")
    def is_ready(request: Request):
        if request.app.state.predict is None:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return {"status": "ready"}

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(registry), headers={"Content-Type": CONTENT_TYPE_LATEST})

    @app.post("/v1/predict", response_model=PredictResponse, response_model_exclude_unset=True)
    def predict(payload: PredictRequest, request: Request):
        state = request.app.state
        kind = payload.analysis_kind
        if state.predict is None:
            count.labels(kind, "not_ready").inc()
            raise HTTPException(status_code=503, detail="Model not ready")
        if kind not in state.kinds:
            count.labels(kind, "unsupported").inc()
            raise HTTPException(status_code=422, detail="Analysis kind is disabled")
        try:
            with latency.labels(kind).time():
                result = state.predict(kind, payload.domain_id, payload.model_text)
            prediction = _validated_prediction(kind, result)
        except Exception:
            count.labels(kind, "error").inc()
            # The backend exception may contain customer data.
            LOGGER.error("VoC prediction failed for analysis kind %s", kind)
            raise HTTPException(status_code=500, detail="Prediction failed") from None
        count.labels(kind, "held" if prediction["label"] is None else "success").inc()
        return {
            "request_id": payload.request_id,
            "analysis_kind": kind,
            "model_version": state.model_version,
            **prediction,
        }

    return app


app = create_app()
