"""Dispatch Silver feature revisions and process one leased canary job at a time."""
import argparse
import hashlib
import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


SENTIMENTS = {"긍정", "중립", "부정", "매우 부정"}
FIELDS = {"sentiment": ("sentiment", "sentiment_status"), "category": ("category_id", "category_status")}
LEASE_SECONDS = 45
HTTP_TIMEOUT = 10
MAX_ATTEMPTS = 3
READY = Path("/tmp/worker-ready")


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    uses_customer_features: bool = False

    @classmethod
    def load(cls, prefix):
        version = os.environ[prefix + "_MODEL_VERSION"].strip()
        base = os.environ[prefix + "_URL"].rstrip("/")
        parsed = urlsplit(base)
        if (not version or parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError(f"Invalid {prefix} release")
        return cls(version, base, os.environ.get(prefix + "_CUSTOMER_FEATURES") == "1")


@dataclass(frozen=True)
class Config:
    company: UUID
    percent: int
    stable: Release
    canary: Release
    category_percent: int = 0
    guard_replays: bool = False

    @classmethod
    def load(cls):
        percent = int(os.environ.get("SENTIMENT_CANARY_PERCENT", "0"))
        category_percent = int(os.environ.get("CATEGORY_CANARY_PERCENT", "0"))
        if not all(0 <= value <= 100 for value in (percent, category_percent)):
            raise ValueError("Canary percentages must be 0..100")
        return cls(UUID(os.environ["DEMO_COMPANY_UUID"]), percent, Release.load("STABLE"), Release.load("CANARY"),
                   category_percent, os.environ.get("VOC_GUARD_REPLAYS") == "1")


def bucket(company, event):
    key = f"{company}:{event}".encode("ascii")
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "big") % 100


def scope_key(kind, domain):
    return "sentiment" if kind == "sentiment" else f"category/{domain}"


def routing_percent(cn, config, scope, configured):
    key = (config.company, scope, config.canary.version)
    cn.execute("""INSERT INTO voc.classification_rollout(company_id,scope_key,candidate_version,stable_version,configured_percent)
        VALUES (%s,%s,%s,%s,%s) ON CONFLICT (company_id,scope_key,candidate_version) DO UPDATE
        SET stable_version=excluded.stable_version,configured_percent=excluded.configured_percent
        WHERE (classification_rollout.stable_version,classification_rollout.configured_percent)
              IS DISTINCT FROM (excluded.stable_version,excluded.configured_percent)""", (*key,config.stable.version,configured))
    row = cn.execute("""SELECT tripped_at FROM voc.classification_rollout
        WHERE company_id=%s AND scope_key=%s AND candidate_version=%s FOR SHARE""", key).fetchone()
    return 0 if row["tripped_at"] else configured


def observe_canary(cn, job, scope, failed, config):
    if job["route"] != "canary":
        return
    if not config.guard_replays and cn.execute("""SELECT 1 FROM voc.bronze_message
            WHERE event_id=%s AND (context_json->>'synthetic'='true' OR origin='synthetic-canary')""",
            (job["event_id"],)).fetchone():
        return
    key = (job["company_id"], scope, job["model_version"])
    state = cn.execute("""SELECT tripped_at,stable_version,to_regclass('voc.model_review_policy') AS policies FROM voc.classification_rollout
        WHERE company_id=%s AND scope_key=%s AND candidate_version=%s FOR UPDATE""", key).fetchone()
    if not state or state["tripped_at"]:
        return
    if not failed:
        cn.execute("""UPDATE voc.classification_rollout SET failures='{}',first_failure_at=NULL
            WHERE company_id=%s AND scope_key=%s AND candidate_version=%s AND failures<>'{}'""", key)
    else:
        record_failure(cn, key, job["event_id"])
    if state["policies"]:
        enforce_saved_limits(cn, key, state["stable_version"], config.guard_replays)


def record_failure(cn, key, event):
    # Duplicate retries of one VOC cannot trip the circuit. Success starts a new streak.
    cn.execute("""WITH observed AS (
        SELECT clock_timestamp() AS at, failures FROM voc.classification_rollout
        WHERE company_id=%s AND scope_key=%s AND candidate_version=%s
    ), events AS (
        SELECT e.key, e.value::timestamptz AS at FROM observed o,jsonb_each_text(o.failures) e
        WHERE e.value::timestamptz > o.at-interval '30 minutes'
        UNION ALL SELECT %s::text,at FROM observed
    ), distinct_events AS (SELECT key,min(at) AS at FROM events GROUP BY key),
    streak AS (SELECT jsonb_object_agg(key,at) AS events,min(at) AS first,count(*) AS n FROM distinct_events)
    UPDATE voc.classification_rollout SET failures=s.events,first_failure_at=s.first,
        tripped_at=CASE WHEN s.n>=3 THEN clock_timestamp() END,
        reason=CASE WHEN s.n>=3 THEN 'consecutive_failures' END
    FROM streak s WHERE company_id=%s AND scope_key=%s AND candidate_version=%s""", (*key,event,*key))


def enforce_saved_limits(cn, key, stable_version, include_replays):
    company, scope, candidate = key
    policy = cn.execute("""SELECT settings FROM voc.model_review_policy WHERE company_id=%s AND scope_key=%s
        AND settings->>'baseline_served'=%s AND settings->>'candidate_served'=%s""",
        (company,scope,stable_version,candidate)).fetchone()
    if not policy:
        return
    limits = policy["settings"]
    kind, _, domain = scope.partition("/")
    rows = cn.execute("""SELECT a.assigned_route AS route,count(*) AS calls,
        100.0*count(*) FILTER (WHERE a.outcome IN ('failed','expired'))/count(*) AS errors,
        CASE WHEN count(a.http_ms)=count(*) THEN percentile_cont(.95) WITHIN GROUP (ORDER BY a.http_ms) END AS p95,
        bool_or(a.served_version IS NOT NULL AND a.served_version<>a.assigned_version OR
            a.assigned_version<>CASE a.assigned_route WHEN 'canary' THEN %s ELSE %s END) AS mixed
        FROM voc.classification_attempt a JOIN voc.classification_job j USING(request_id)
        JOIN public.ticket t ON t.company_id=j.company_id AND t.source_event_id=j.event_id
        JOIN voc.bronze_message b ON b.event_id=j.event_id
        WHERE j.company_id=%s AND j.analysis_kind=%s AND (%s::uuid IS NULL OR t.domain_id=%s::uuid)
          AND a.started_at>clock_timestamp()-interval '30 minutes' AND a.finished_at IS NOT NULL
          AND (%s OR (b.origin IS DISTINCT FROM 'synthetic-canary' AND b.context_json->>'synthetic' IS DISTINCT FROM 'true'))
        GROUP BY a.assigned_route""", (candidate, stable_version, company, kind, domain or None, domain or None, include_replays)).fetchall()
    lanes = {row["route"]: row for row in rows}
    base, canary = lanes.get("stable"), lanes.get("canary")
    # Existing partial policies inherit the same defaults as the administrator's review.
    minimum = limits.get("min_calls", 100)
    error_limit = limits.get("error_limit", 1)
    if not canary or canary["calls"] < minimum or any(row["mixed"] for row in rows):
        return
    base_ready = base and base["calls"] >= minimum
    if base_ready and base["errors"] > error_limit:
        return
    reason = None
    if canary["errors"] > error_limit:
        reason = "candidate_error_rate"
    elif canary["p95"] is not None:
        absolute = limits.get("absolute_p95")
        if ((absolute is not None and canary["p95"] > absolute) or
                (base_ready and base["p95"] and canary["p95"] > base["p95"] * limits.get("latency_limit", 1.5))):
            reason = "candidate_p95"
    if reason:
        cn.execute("""UPDATE voc.classification_rollout SET tripped_at=clock_timestamp(),reason=%s
            WHERE company_id=%s AND scope_key=%s AND candidate_version=%s AND tripped_at IS NULL""", (reason,*key))


def dispatch(cn, config):
    """Hand off an outbox row and mark it published atomically; never evaluate unscoped rows."""
    with cn.transaction():
        row = cn.execute("""
            SELECT company_id,event_id,preprocess_version,body_version,analysis_kind,queued_at
            FROM voc.ticket_analysis_outbox WHERE company_id=%s AND published_at IS NULL
            ORDER BY queued_at,event_id,analysis_kind LIMIT 1 FOR UPDATE SKIP LOCKED
        """, (config.company,)).fetchone()
        if row is None:
            return False
        ticket = cn.execute("SELECT domain_id FROM public.ticket WHERE company_id=%s AND source_event_id=%s",
                            (config.company, row["event_id"])).fetchone()
        configured = config.percent if row["analysis_kind"] == "sentiment" else config.category_percent
        percent = routing_percent(cn, config, scope_key(row["analysis_kind"], ticket["domain_id"]), configured) if ticket else 0
        canary = bucket(config.company, row["event_id"]) < percent
        release = config.canary if canary else config.stable
        if canary and release.uses_customer_features and not config.stable.uses_customer_features:
            snapshot = cn.execute("""
                SELECT 1 FROM voc.feature_snapshot
                WHERE company_id=%(company_id)s AND silver_event_id=%(event_id)s
                  AND preprocess_version=%(preprocess_version)s AND body_version=%(body_version)s
            """, row).fetchone()
            if snapshot is None:
                canary, release = False, config.stable
        cn.execute("""
            INSERT INTO voc.classification_job
                (company_id,event_id,preprocess_version,body_version,analysis_kind,queued_at,route,model_version,model_url,uses_customer_features)
            VALUES (%(company_id)s,%(event_id)s,%(preprocess_version)s,%(body_version)s,%(analysis_kind)s,
                    %(queued_at)s,%(route)s,%(model_version)s,%(model_url)s,%(uses_customer_features)s)
            ON CONFLICT (company_id,event_id,preprocess_version,body_version,analysis_kind) DO NOTHING
        """, {**row, "route": "canary" if canary else "stable", "model_version": release.version, "model_url": release.url,
            "uses_customer_features": release.uses_customer_features})
        cn.execute("""
            UPDATE voc.ticket_analysis_outbox SET published_at=clock_timestamp()
            WHERE company_id=%(company_id)s AND event_id=%(event_id)s AND analysis_kind=%(analysis_kind)s
              AND preprocess_version=%(preprocess_version)s AND body_version=%(body_version)s
        """, row)
        return True


def current_ticket(cn, job):
    # Same lock order as Silver projection (Silver first, then ticket). Locks last
    # only until the claim/result transaction commits, never across network I/O.
    silver = cn.execute("""
        SELECT body_version,message_kind FROM voc.silver_message
        WHERE event_id=%(event_id)s AND schema_version=4 FOR SHARE
    """, job).fetchone()
    if not silver or silver["body_version"] != job["body_version"] or silver["message_kind"] != "customer_inquiry":
        return None
    value, status = map(sql.Identifier, FIELDS[job["analysis_kind"]])
    return cn.execute(sql.SQL("""
        SELECT t.ticket_id,t.domain_id,t.{}::text AS current_value,t.{} AS current_status
        FROM public.ticket t WHERE t.company_id=%(company_id)s AND t.source_event_id=%(event_id)s
          AND (%(analysis_kind)s <> 'category' OR NOT t.is_category_manual)
          AND NOT EXISTS (SELECT FROM voc.model_text_history h WHERE h.company_id=t.company_id
                AND h.event_id=t.source_event_id AND h.body_version=%(body_version)s
                AND h.preprocess_version>%(preprocess_version)s)
        FOR UPDATE OF t
    """).format(value, status), job).fetchone()


def project(cn, job, ticket, value, status):
    if ticket is None:
        return False
    # current_ticket already holds the row lock. Read the manual event after acquiring it.
    if job["analysis_kind"] == "sentiment" and cn.execute("""
        SELECT 1 FROM public.ticket_event e JOIN public.event_type t USING(event_type_id)
        WHERE e.ticket_id=%s AND t.code='ticket_sentiment_changed'
    """, (ticket["ticket_id"],)).fetchone():
        return False
    marker = cn.execute("""
        SELECT value,status,request_id FROM voc.classification_projection WHERE ticket_id=%s AND analysis_kind=%s
    """, (ticket["ticket_id"], job["analysis_kind"])).fetchone()
    expected = (marker["value"], marker["status"]) if marker else (None, "pending")
    if (ticket["current_value"], ticket["current_status"]) != expected:
        return False
    if status != "running" and (not marker or marker["request_id"] != job["request_id"]):
        return False
    field, state = map(sql.Identifier, FIELDS[job["analysis_kind"]])
    cn.execute(sql.SQL("UPDATE public.ticket SET {}=%s, {}=%s, updated_at=clock_timestamp() WHERE ticket_id=%s")
               .format(field, state), (value, status, ticket["ticket_id"]))
    cn.execute("""
        INSERT INTO voc.classification_projection(ticket_id,analysis_kind,request_id,value,status)
        VALUES (%s,%s,%s,%s,%s) ON CONFLICT(ticket_id,analysis_kind) DO UPDATE
        SET request_id=excluded.request_id,value=excluded.value,status=excluded.status
    """, (ticket["ticket_id"], job["analysis_kind"], job["request_id"], str(value) if value is not None else None, status))
    return True


def terminate(cn, job, state, *, label=None, applied=False):
    cn.execute("""
        UPDATE voc.classification_job SET state=%s, finished_at=clock_timestamp(), label=%s, applied=%s
        WHERE request_id=%s
    """, (state, label, applied, job["request_id"]))


def pause_withdrawn(cn, job):
    # An already-published outbox row is retained when a role changes. If the
    # same revision is later confirmed again, its original job must resume.
    withdrawn = cn.execute("""
        SELECT 1 FROM voc.silver_message WHERE event_id=%(event_id)s AND schema_version=4
        AND body_version=%(body_version)s AND message_kind<>'customer_inquiry'
    """, job).fetchone()
    if withdrawn:
        cn.execute("UPDATE voc.classification_job SET state='pending',ready_at=now() WHERE request_id=%s",
                   (job["request_id"],))
    return bool(withdrawn)


def claim(cn, config):
    """Return (did_work, job). Expired labels, roles and leases are handled before HTTP."""
    with cn.transaction():
        job = cn.execute("""
            SELECT j.* FROM voc.classification_job j JOIN voc.silver_message s ON s.event_id=j.event_id AND s.schema_version=4
            WHERE j.company_id=%s AND j.state IN ('pending','running') AND j.ready_at<=clock_timestamp()
              AND (s.message_kind='customer_inquiry' OR s.body_version<>j.body_version)
            ORDER BY j.ready_at,j.queued_at,j.request_id LIMIT 1 FOR UPDATE OF j SKIP LOCKED
        """, (config.company,)).fetchone()
        if job is None:
            return False, None
        ticket = current_ticket(cn, job)
        scope = scope_key(job["analysis_kind"], ticket["domain_id"]) if ticket else None
        if job["state"] == "running":
            cn.execute("""
                UPDATE voc.classification_attempt SET finished_at=clock_timestamp(),outcome='expired',error_code='lease_expired'
                WHERE request_id=%(request_id)s AND attempt_no=%(attempt_count)s AND finished_at IS NULL
            """, job)
            if scope:
                observe_canary(cn, job, scope, True, config)
        if ticket is None and pause_withdrawn(cn, job):
            return True, None
        if job["attempt_count"] >= MAX_ATTEMPTS:
            applied = project(cn, job, ticket, None, "failed")
            terminate(cn, job, "failed", applied=applied)
            return True, None
        if not project(cn, job, ticket, None, "running"):
            terminate(cn, job, "skipped")
            return True, None
        if job["route"] == "canary":
            guard = cn.execute("""SELECT tripped_at FROM voc.classification_rollout
                WHERE company_id=%s AND scope_key=%s AND candidate_version=%s FOR SHARE""",
                (config.company,scope,job["model_version"])).fetchone()
            if guard and guard["tripped_at"]:
                # Old workers running during migration may have started attempts without a target snapshot.
                cn.execute("""UPDATE voc.classification_attempt SET assigned_route=%(route)s,assigned_version=%(model_version)s
                    WHERE request_id=%(request_id)s AND assigned_route IS NULL""",job)
                job.update(route="stable", model_version=config.stable.version, model_url=config.stable.url,
                           uses_customer_features=config.stable.uses_customer_features)
                cn.execute("""UPDATE voc.classification_job SET route=%(route)s,model_version=%(model_version)s,
                    model_url=%(model_url)s,uses_customer_features=%(uses_customer_features)s
                    WHERE request_id=%(request_id)s""", job)
        job["attempt_count"] += 1
        cn.execute("""
            UPDATE voc.classification_job SET state='running', attempt_count=%s,
                ready_at=clock_timestamp()+%s*interval '1 second' WHERE request_id=%s
        """, (job["attempt_count"], LEASE_SECONDS, job["request_id"]))
        cn.execute("""
            INSERT INTO voc.classification_attempt(request_id,attempt_no,assigned_route,assigned_version) VALUES(%s,%s,%s,%s)
        """, (job["request_id"], job["attempt_count"],job["route"],job["model_version"]))
        feature = cn.execute("""
            SELECT model_text FROM voc.model_text_history WHERE company_id=%(company_id)s AND event_id=%(event_id)s
              AND preprocess_version=%(preprocess_version)s AND body_version=%(body_version)s
        """, job).fetchone()
        job.update(domain_id=str(ticket["domain_id"]), model_text=feature["model_text"])
        if job["uses_customer_features"]:
            job["snapshot"] = cn.execute("""
                SELECT feature_snapshot_id,title,body,unresolved_count,avg_first_reply_7d_ms / 1000 AS avg_first_reply_7d_seconds,available_at
                FROM voc.feature_snapshot
                WHERE company_id=%(company_id)s AND silver_event_id=%(event_id)s
                  AND preprocess_version=%(preprocess_version)s AND body_version=%(body_version)s
                  AND available_at<=clock_timestamp()
            """, job).fetchone()
            if job["snapshot"]:
                cn.execute("""
                    UPDATE voc.classification_job SET feature_snapshot_id=%s WHERE request_id=%s
                """, (job["snapshot"]["feature_snapshot_id"],job["request_id"]))
        return True, job


class PredictionError(Exception):
    def __init__(self, code, served_version=None):
        super().__init__(code)
        self.code = code
        self.served_version = served_version


def billed_cost(headers):
    value = headers.get("X-Inference-Cost-KRW") if headers else None
    if not value:
        return None
    try:
        cost = Decimal(value)
        return cost if cost.is_finite() and 0 <= cost <= 10**12 and cost.as_tuple().exponent >= -12 else None
    except InvalidOperation:
        return None


def predict(job):
    request_id = str(job["request_id"])
    body = {"request_id": request_id, "analysis_kind": job["analysis_kind"],
            "model_text": job["model_text"], "domain_id": job["domain_id"]}
    if job.get("uses_customer_features"):
        from online_features import read_online

        if not job["snapshot"]:
            raise PredictionError("missing_feature_snapshot")
        try:
            online = read_online(job["snapshot"])
        except Exception:
            raise PredictionError("feature_store_error") from None
        body.update(model_text=online.pop("body"),title=online.pop("title"),customer_features=online)
    request = Request(job["model_url"] + "/v1/predict", json.dumps(body).encode(), {"Content-Type": "application/json"})
    started = time.perf_counter()
    try:
        try:
            with urlopen(request, timeout=HTTP_TIMEOUT) as response:
                raw = response.read(65537)
                job["cost_krw"] = billed_cost(getattr(response, "headers", None))
        finally:
            job["http_ms"] = (time.perf_counter() - started) * 1000
        if len(raw) > 65536:
            raise PredictionError("response_too_large")
        result = json.loads(raw)
    except HTTPError as exc:
        job["cost_krw"] = billed_cost(exc.headers)
        raise PredictionError(f"http_{exc.code}") from None
    except (OSError, URLError) as exc:
        raise PredictionError("transport_error") from exc
    except (UnicodeError, ValueError):
        raise PredictionError("invalid_json") from None
    if not isinstance(result, dict) or result.get("request_id") != request_id or result.get("analysis_kind") != job["analysis_kind"]:
        raise PredictionError("response_mismatch")
    version = result.get("model_version")
    if version != job["model_version"]:
        raise PredictionError("model_version_mismatch", version if isinstance(version, str) and len(version) <= 100 else None)
    if "label" not in result:
        raise PredictionError("missing_label", version)
    label = result["label"]
    if job["analysis_kind"] == "sentiment":
        valid = isinstance(label, str) and label in SENTIMENTS
    else:
        valid = label is None or (isinstance(label, str) and bool(label.strip()) and len(label) <= 128)
    if not valid:
        raise PredictionError("invalid_label", version)
    return label


def finish(cn, job, label=None, error=None, *, config=None):
    with cn.transaction():
        locked = cn.execute("""
            SELECT state,attempt_count FROM voc.classification_job WHERE request_id=%s FOR UPDATE
        """, (job["request_id"],)).fetchone()
        if locked["state"] != "running" or locked["attempt_count"] != job["attempt_count"]:
            return  # A newer lease owns both the ticket write and its attempt history.
        ticket = current_ticket(cn, job)
        value = label
        if error is None and job["analysis_kind"] == "category" and label is not None:
            mapping = cn.execute("""
                SELECT category_id FROM voc.classification_category_map WHERE domain_id=%s AND label=%s
            """, (job["domain_id"], label)).fetchone()
            if mapping is None:
                error = PredictionError("unmapped_category", job["model_version"])
            else:
                value = mapping["category_id"]
        cn.execute("""
            UPDATE voc.classification_attempt SET finished_at=clock_timestamp(), outcome=%s, error_code=%s, served_version=%s,
                http_ms=%s,cost_krw=%s
            WHERE request_id=%s AND attempt_no=%s
        """, ("failed" if error else "succeeded", error.code if error else None,
              error.served_version if error else job["model_version"], job.get("http_ms"), job.get("cost_krw"),
              job["request_id"], job["attempt_count"]))
        if config and ticket:
            observe_canary(cn, job, scope_key(job["analysis_kind"], ticket["domain_id"]), error is not None, config)
        if ticket is None and pause_withdrawn(cn, job):
            return
        if error is None:
            applied = project(cn, job, ticket, value, "succeeded")
            terminate(cn, job, "succeeded", label=label, applied=applied)
        elif ticket is None:
            terminate(cn, job, "skipped")
        elif job["attempt_count"] >= MAX_ATTEMPTS:
            applied = project(cn, job, ticket, None, "failed")
            terminate(cn, job, "failed", applied=applied)
        else:
            cn.execute("""
                UPDATE voc.classification_job SET state='pending', ready_at=clock_timestamp()+%s*interval '1 second'
                WHERE request_id=%s
            """, (2 ** (job["attempt_count"] - 1), job["request_id"]))


def process_one(cn, config):
    queued = dispatch(cn, config)
    claimed, job = claim(cn, config)
    if job is not None:
        try:
            label = predict(job)
        except PredictionError as exc:
            logging.warning("Inference failed request_id=%s code=%s", job["request_id"], exc.code)
            finish(cn, job, error=exc, config=config)
        else:
            finish(cn, job, label=label, config=config)
    return queued or claimed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["migrate", "verify-schema", "run", "once"])
    action = parser.parse_args().command
    if action == "run":
        READY.unlink(missing_ok=True)
    password_file = os.getenv("PGPASSWORD_FILE")
    credentials = {"password": Path(password_file).read_text().strip()} if password_file else {}
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row, connect_timeout=5, **credentials) as cn:
        if action in {"migrate", "verify-schema"}:
            script = "schema.sql" if action == "migrate" else "verify-schema.sql"
            with cn.transaction():
                cn.execute(Path(__file__).with_name(script).read_text())
            return
        config = Config.load()
        stopping = False

        def drain(signum, frame):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, drain)
        signal.signal(signal.SIGINT, drain)
        while not stopping:
            worked = process_one(cn, config)
            if action == "once":
                return
            if not READY.exists():
                READY.touch()
            if not worked:
                time.sleep(0.5)


if __name__ == "__main__":
    main()
