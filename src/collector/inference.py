"""Call deployed models, preserve partial results and retry durable analysis work."""
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from .observations import fail, heartbeat, observe, recover
from .pipeline import db

SENTIMENTS = {"긍정", "중립", "부정", "매우 부정"}
KINDS = ("sentiment", "category")
TIMEOUT = 15
RESULT_LIMIT = 1024 * 1024
MODEL_PART = re.compile(r"^[\w./:@+-]{1,128}$")
PERSONAL_DATA = re.compile(
    r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    r"|(?<!\d)(?:\+82[- .]?)?(?:0?1[016789]|0[2-6]\d?)[- .]?\d{3,4}[- .]?\d{4}(?!\d)"
    r"|(?<!\d)\d{6}[- ]?[1-8]\d{6}(?!\d)"
)


def endpoint(api, preferred="stable"):
    if api == "embedding":
        return os.getenv("VOC_EMBEDDING_URL", "")
    return os.getenv("VOC_CLASSIFIER_" + preferred.upper() + "_URL", "")


def initial_route(event):
    if endpoint("classification", "router"):
        return "router"
    share = int(os.getenv("VOC_CANARY_PERCENT", "0"))
    if not 0 <= share <= 100:
        raise ValueError("VOC_CANARY_PERCENT must be between 0 and 100")
    bucket = int.from_bytes(hashlib.sha256(str(event).encode()).digest()[:8], "big") % 100
    return "canary" if endpoint("classification", "canary") and bucket < share else "stable"


def model_identifier(value):
    return value if isinstance(value, str) and MODEL_PART.fullmatch(value) else None


@dataclass
class Result:
    elapsed_ms: float
    model_name: str | None = None
    model_version: str | None = None
    rollout: str | None = None
    model_ms: float | None = None
    status: int | None = None
    error: str | None = None
    payload: dict = field(default_factory=dict)


def call(url, payload):
    request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
    token = os.getenv("VOC_MODEL_API_TOKEN")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    started = time.perf_counter()
    info, headers, status, error = {}, {}, None, None
    try:
        try:
            response = urlopen(request, timeout=TIMEOUT)
        except HTTPError as exc:
            response = exc
        with response:
            status, headers = response.status, response.headers
            if not 200 <= status < 300:
                error = "http_error"
            raw = response.read(RESULT_LIMIT + 1)
            if len(raw) <= RESULT_LIMIT:
                info = json.loads(raw)
            if not isinstance(info, dict):
                info = {}
                error = error or "invalid_response"
            if len(raw) > RESULT_LIMIT:
                error = error or "response_too_large"
    except (TimeoutError, URLError, OSError, HTTPException):
        error = error or "transport_error"
    except (ValueError, UnicodeError):
        error = error or "invalid_response"
    ms = info.get("model_ms")
    return Result(
        elapsed_ms=(time.perf_counter() - started) * 1000,
        model_name=model_identifier(headers.get("X-Model-Name") or info.get("model_name")),
        model_version=model_identifier(headers.get("X-Model-Version") or info.get("model_version")),
        rollout=(route if (route := headers.get("X-Model-Rollout") or info.get("rollout")) in ("stable", "canary") else None),
        model_ms=ms if type(ms) in (int, float) and math.isfinite(ms) and ms >= 0 else None,
        status=status, error=error, payload=info,
    )


def validated_predictions(cn, domain, payload, kinds):
    predictions = {}
    if "sentiment" in kinds and isinstance(payload.get("sentiment"), str) and payload["sentiment"] in SENTIMENTS:
        predictions["sentiment"] = payload["sentiment"]
    if "category" in kinds and isinstance(payload.get("category_id"), str):
        try:
            category = UUID(payload["category_id"])
        except ValueError:
            return predictions
        if cn.execute("SELECT 1 FROM public.category WHERE domain_uuid=%s AND category_uuid=%s",
                      (domain, category)).fetchone():
            predictions["category"] = str(category)
    return predictions


def vector(payload):
    values = payload.get("embedding")
    if (isinstance(values, list) and 0 < len(values) <= 16000
            and all(type(n) in (int, float) and -3.4e38 < n < 3.4e38 for n in values)):
        return json.dumps(values, separators=(",", ":"))
    return None


def redact(text, *identifiers):
    text = PERSONAL_DATA.sub("[개인정보]", text or "")
    for value in identifiers:
        if value and len(value.strip()) > 1:
            text = re.sub(re.escape(value), "[개인정보]", text, flags=re.IGNORECASE)
    return text


def start_call(cn, attempt, api, traffic, route, ticket=None, body_version=None):
    return cn.execute("""
        INSERT INTO voc.inference_attempt(attempt_id,ticket_uuid,body_version,api,traffic,preferred_route)
        VALUES (%s,%s,%s,%s,%s,%s) RETURNING started_at
    """, (attempt, ticket, body_version, api, traffic, route)).fetchone()["started_at"]


def finish_call(cn, attempt, end, result):
    cn.execute("""
        UPDATE voc.inference_attempt
        SET ended_at=%s,model_name=%s,model_version=%s,rollout=%s,http_status=%s,response_ms=%s,model_ms=%s,error=%s
        WHERE attempt_id=%s AND ended_at IS NULL
    """, (end, result.model_name, result.model_version, result.rollout, result.status, result.elapsed_ms,
          result.model_ms, result.error, attempt))


def claim(cn, api):
    kind = "sentiment" if api == "classification" else api
    with cn.transaction():
        work = cn.execute("""
            SELECT q.*,t.ticket_uuid,t.domain_uuid,t.sentiment_status,t.category_status,
                   t.embedding IS NOT NULL AS has_embedding,t.classification_final_at,t.status,h.model_text,coalesce(h.model_title,s.title,'') AS model_title,
                   s.sender_ref,s.sender_display_name
            FROM voc.ticket_analysis_outbox q
            JOIN voc.model_text_history h USING(company_uuid,event_id,preprocess_version,body_version)
            JOIN voc.silver_messages s ON s.event_id=q.event_id AND s.schema_version=4 AND s.body_version=q.body_version
            JOIN public.ticket t ON t.source_event_id=q.event_id
            WHERE q.analysis_kind=%s AND s.message_kind='customer_inquiry' AND q.finished_at IS NULL
              AND q.available_at<=clock_timestamp() AND (q.lease_until IS NULL OR q.lease_until<clock_timestamp())
            ORDER BY q.queued_at,q.event_id LIMIT 1 FOR UPDATE OF q,t SKIP LOCKED
        """, (kind,)).fetchone()
        if not work:
            return None
        work["api"] = api
        work["missing"] = [name for name in KINDS if work[name + "_status"] != "succeeded"]
        # Old completed results and current staff values are preserved during upgrade.
        if (api == "classification" and (not work["missing"] or work["status"] == "답변완료"
                                        or work["classification_final_at"] is not None)
                or api == "embedding" and work["has_embedding"]):
            close_work(cn, work)
            return None
        if work["claim_token"]:
            cn.execute("""UPDATE voc.inference_attempt SET error='unknown_outcome'
                          WHERE attempt_id=%s AND ended_at IS NULL""", (work["claim_token"],))
            fail(cn, api, str(work["claim_token"]), work["event_id"], "worker_interrupted")
        work["total_attempts"] = work["attempt_count"]
        if api == "classification":
            work["total_attempts"] = cn.execute("""
                SELECT count(*) FROM voc.inference_attempt
                WHERE ticket_uuid=%s AND api='classification' AND traffic='operational'
            """, (work["ticket_uuid"],)).fetchone()["count"]
        if work["total_attempts"] >= work["retry_budget"]:
            if api == "classification":
                finalize_classification(cn, work)
            close_work(cn, work)
            return None
        work["route"] = initial_route(work["event_id"]) if api == "classification" and work["total_attempts"] == 0 else (
            "stable" if api == "classification" else "embedding")
        work["claim_token"] = uuid4()
        work["attempt_count"] += 1
        work["total_attempts"] += 1
        cn.execute("""
            UPDATE voc.ticket_analysis_outbox SET claim_token=%(claim_token)s,attempt_count=%(attempt_count)s,
                   lease_until=clock_timestamp()+interval '2 minutes'
            WHERE (event_id,preprocess_version,body_version,analysis_kind)=
                  (%(event_id)s,%(preprocess_version)s,%(body_version)s,%(analysis_kind)s)
        """, work)
        started = start_call(cn, work["claim_token"], api, "operational", work["route"],
                             work["ticket_uuid"], work["body_version"])
        observe(cn, work["event_id"], "compute", started)
        return work


def close_work(cn, work):
    cn.execute("""
        UPDATE voc.ticket_analysis_outbox
        SET finished_at=clock_timestamp(),claim_token=NULL,lease_until=NULL
        WHERE (event_id,preprocess_version,body_version)=(%(event_id)s,%(preprocess_version)s,%(body_version)s)
          AND (analysis_kind=%(analysis_kind)s OR (%(analysis_kind)s='sentiment' AND analysis_kind='category'))
    """, work)


def finalize_classification(cn, work):
    cn.execute("""
        UPDATE public.ticket SET classification_final_at=coalesce(classification_final_at,clock_timestamp()),
               sentiment_status=CASE WHEN sentiment_status='succeeded' THEN sentiment_status ELSE 'failed' END,
               category_status=CASE WHEN category_status='succeeded' THEN category_status ELSE 'failed' END
        WHERE ticket_uuid=%(ticket_uuid)s
    """, work)


def save_classification(cn, work, predictions):
    for kind, value in predictions.items():
        inserted = cn.execute("""
            INSERT INTO voc.ticket_prediction(ticket_uuid,kind,value,attempt_id)
            VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING
        """, (work["ticket_uuid"], kind, value, work["claim_token"])).rowcount
        if inserted and kind == "sentiment":
            cn.execute("UPDATE public.ticket SET sentiment=%s,sentiment_status='succeeded' WHERE ticket_uuid=%s",
                       (value, work["ticket_uuid"]))
        elif inserted:
            cn.execute("UPDATE public.ticket SET category_uuid=%s,category_status='succeeded' WHERE ticket_uuid=%s",
                       (value, work["ticket_uuid"]))


def finish(cn, work, result, ended):
    api, event, attempt = work["api"], work["event_id"], work["claim_token"]
    with cn.transaction():
        current = cn.execute("""
            SELECT q.claim_token,s.body_version,s.message_kind,t.status
            FROM voc.ticket_analysis_outbox q JOIN voc.silver_messages s ON s.event_id=q.event_id AND s.schema_version=4
            JOIN public.ticket t ON t.source_event_id=q.event_id
            WHERE (q.event_id,q.preprocess_version,q.body_version,q.analysis_kind)=
                  (%(event_id)s,%(preprocess_version)s,%(body_version)s,%(analysis_kind)s)
            FOR UPDATE OF q,t
        """, work).fetchone()
        predictions, embedding = {}, None
        if not result.error:
            if not (result.model_name and result.model_version and (api == "embedding" or result.rollout)):
                result.error = "model_identity_missing"
            elif api == "classification":
                predictions = validated_predictions(cn, work["domain_uuid"], result.payload, work["missing"])
                if len(predictions) != len(work["missing"]):
                    result.error = "incomplete_classification"
            else:
                embedding = vector(result.payload)
                if embedding is None:
                    result.error = "invalid_embedding"
        finish_call(cn, attempt, ended, result)
        if not current or current["claim_token"] != attempt:
            return
        if current["body_version"] != work["body_version"] or current["message_kind"] != "customer_inquiry":
            close_work(cn, work)
            return
        if api == "classification" and current["status"] == "답변완료":
            close_work(cn, work)
            return
        if predictions:
            save_classification(cn, work, predictions)
        if embedding is not None:
            cn.execute("""
                UPDATE public.ticket SET embedding=%s::vector,embedding_model_name=%s,
                       embedding_model_version=%s,embedding_body_version=%s,embedding_attempt_id=%s
                WHERE ticket_uuid=%s AND embedding IS NULL
            """, (embedding, result.model_name, result.model_version, work["body_version"], attempt, work["ticket_uuid"]))
        if result.error:
            fail(cn, api, str(attempt), event, result.error)
        else:
            recover(cn, api, event)
        if not result.error or work["total_attempts"] >= work["retry_budget"]:
            if api == "classification":
                finalize_classification(cn, work)
            close_work(cn, work)
        else:
            cn.execute("""
                UPDATE voc.ticket_analysis_outbox SET claim_token=NULL,lease_until=NULL,
                       available_at=clock_timestamp()+interval '2 seconds' * attempt_count
                WHERE claim_token=%(claim_token)s
            """, work)
        maybe_ready(cn, work)


def maybe_ready(cn, work):
    row = cn.execute("""
        SELECT max(a.ended_at) AS computed_at
        FROM public.ticket t
        JOIN voc.ticket_prediction s ON s.ticket_uuid=t.ticket_uuid AND s.kind='sentiment'
        JOIN voc.ticket_prediction c ON c.ticket_uuid=t.ticket_uuid AND c.kind='category'
        JOIN voc.inference_attempt a ON a.attempt_id IN (s.attempt_id,c.attempt_id,t.embedding_attempt_id)
        WHERE t.ticket_uuid=%(ticket_uuid)s AND t.ready_at IS NULL AND t.embedding_attempt_id IS NOT NULL
    """, work).fetchone()
    if row["computed_at"] is None:
        return
    computed = row["computed_at"]
    observe(cn, work["event_id"], "compute", end=computed)
    observe(cn, work["event_id"], "ready", computed)
    row = cn.execute("""UPDATE public.ticket SET ready_at=clock_timestamp()
                       WHERE ticket_uuid=%(ticket_uuid)s AND ready_at IS NULL RETURNING ready_at""", work).fetchone()
    if row:
        observe(cn, work["event_id"], "ready", end=row["ready_at"])


def payload(work):
    if work["api"] == "classification":
        return {"title": work["model_title"], "text": work["model_text"], "domain_id": str(work["domain_uuid"]),
                "kinds": work["missing"]}
    identifiers = (work["sender_ref"], work["sender_display_name"])
    return {"title": redact(work["model_title"], *identifiers), "text": redact(work["model_text"], *identifiers)}


def run_worker(api):
    with db() as cn:
        last_heartbeat = 0
        while True:
            # A missing model connection leaves work pending and consumes no retries.
            if not endpoint(api):
                time.sleep(0.5)
                continue
            if time.monotonic() - last_heartbeat >= 15:
                heartbeat(cn, api)
                if api == "classification":
                    heartbeat(cn, "stable_model")
                    if ((endpoint("classification", "canary") and int(os.getenv("VOC_CANARY_PERCENT", "0")) > 0)
                            or (endpoint("classification", "canary") and endpoint("classification", "router"))):
                        heartbeat(cn, "canary_model")
                last_heartbeat = time.monotonic()
            if not (work := claim(cn, api)):
                time.sleep(0.5)
                continue
            result = call(endpoint(api, work["route"]), payload(work))
            finish(cn, work, result, datetime.now(timezone.utc))


def retry_embedding(event):
    with db() as cn:
        updated = cn.execute("""
            UPDATE voc.ticket_analysis_outbox q SET available_at=clock_timestamp(),finished_at=NULL,retry_budget=attempt_count+3
            FROM public.ticket t JOIN voc.silver_messages s ON s.event_id=t.source_event_id AND s.schema_version=4
            WHERE q.event_id=%s AND q.event_id=t.source_event_id AND q.body_version=s.body_version
              AND s.message_kind='customer_inquiry' AND q.analysis_kind='embedding'
              AND q.finished_at IS NOT NULL AND t.embedding IS NULL
        """, (UUID(event),)).rowcount
    print(f"재시도 예약 {updated}건")
