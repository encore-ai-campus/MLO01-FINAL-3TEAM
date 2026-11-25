"""Dispatch Silver feature revisions and process one leased canary job at a time."""
import argparse
import hashlib
import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


SENTIMENTS = {"긍정", "중립", "부정", "매우 부정"}
FIELDS = {"sentiment": ("sentiment", "sentiment_status"), "category": ("category_uuid", "category_status")}
LEASE_SECONDS = 45
HTTP_TIMEOUT = 10
MAX_ATTEMPTS = 3
READY = Path("/tmp/worker-ready")


@dataclass(frozen=True)
class Release:
    version: str
    url: str

    @classmethod
    def load(cls, prefix):
        version = os.environ[prefix + "_MODEL_VERSION"].strip()
        base = os.environ[prefix + "_URL"].rstrip("/")
        parsed = urlsplit(base)
        if (not version or parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError(f"Invalid {prefix} release")
        return cls(version, base)


@dataclass(frozen=True)
class Config:
    company: UUID
    percent: int
    stable: Release
    canary: Release

    @classmethod
    def load(cls):
        percent = int(os.environ.get("SENTIMENT_CANARY_PERCENT", "0"))
        if not 0 <= percent <= 100:
            raise ValueError("SENTIMENT_CANARY_PERCENT must be 0..100")
        return cls(UUID(os.environ["DEMO_COMPANY_UUID"]), percent, Release.load("STABLE"), Release.load("CANARY"))


def bucket(company, event):
    key = f"{company}:{event}".encode("ascii")
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "big") % 100


def dispatch(cn, config):
    """Hand off an outbox row and mark it published atomically; never evaluate unscoped rows."""
    with cn.transaction():
        row = cn.execute("""
            SELECT company_uuid,event_id,preprocess_version,body_version,analysis_kind,queued_at
            FROM voc.ticket_analysis_outbox WHERE company_uuid=%s AND published_at IS NULL
            ORDER BY queued_at,event_id,analysis_kind LIMIT 1 FOR UPDATE SKIP LOCKED
        """, (config.company,)).fetchone()
        if row is None:
            return False
        canary = row["analysis_kind"] == "sentiment" and bucket(config.company, row["event_id"]) < config.percent
        release = config.canary if canary else config.stable
        cn.execute("""
            INSERT INTO voc.classification_job
                (company_uuid,event_id,preprocess_version,body_version,analysis_kind,queued_at,route,model_version,model_url)
            VALUES (%(company_uuid)s,%(event_id)s,%(preprocess_version)s,%(body_version)s,%(analysis_kind)s,
                    %(queued_at)s,%(route)s,%(model_version)s,%(model_url)s)
            ON CONFLICT (company_uuid,event_id,preprocess_version,body_version,analysis_kind) DO NOTHING
        """, {**row, "route": "canary" if canary else "stable", "model_version": release.version, "model_url": release.url})
        cn.execute("""
            UPDATE voc.ticket_analysis_outbox SET published_at=clock_timestamp()
            WHERE company_uuid=%(company_uuid)s AND event_id=%(event_id)s AND analysis_kind=%(analysis_kind)s
              AND preprocess_version=%(preprocess_version)s AND body_version=%(body_version)s
        """, row)
        return True


def current_ticket(cn, job):
    # Same lock order as Silver projection (Silver first, then ticket). Locks last
    # only until the claim/result transaction commits, never across network I/O.
    silver = cn.execute("""
        SELECT body_version,message_kind FROM voc.silver_messages
        WHERE event_id=%(event_id)s AND schema_version=4 FOR SHARE
    """, job).fetchone()
    if not silver or silver["body_version"] != job["body_version"] or silver["message_kind"] != "customer_inquiry":
        return None
    value, status = map(sql.Identifier, FIELDS[job["analysis_kind"]])
    return cn.execute(sql.SQL("""
        SELECT t.ticket_uuid,t.domain_uuid,t.{}::text AS current_value,t.{} AS current_status
        FROM public.ticket t WHERE t.company_uuid=%(company_uuid)s AND t.source_event_id=%(event_id)s
          AND (%(analysis_kind)s <> 'category' OR NOT t.category_manually_set)
          AND NOT EXISTS (SELECT FROM voc.model_text_history h WHERE h.company_uuid=t.company_uuid
                AND h.event_id=t.source_event_id AND h.body_version=%(body_version)s
                AND h.preprocess_version>%(preprocess_version)s)
        FOR UPDATE OF t
    """).format(value, status), job).fetchone()


def project(cn, job, ticket, value, status):
    if ticket is None:
        return False
    marker = cn.execute("""
        SELECT value,status,request_id FROM voc.classification_projection WHERE ticket_uuid=%s AND analysis_kind=%s
    """, (ticket["ticket_uuid"], job["analysis_kind"])).fetchone()
    expected = (marker["value"], marker["status"]) if marker else (None, "pending")
    if (ticket["current_value"], ticket["current_status"]) != expected:
        return False
    if status != "running" and (not marker or marker["request_id"] != job["request_id"]):
        return False
    field, state = map(sql.Identifier, FIELDS[job["analysis_kind"]])
    cn.execute(sql.SQL("UPDATE public.ticket SET {}=%s, {}=%s, updated_at=clock_timestamp() WHERE ticket_uuid=%s")
               .format(field, state), (value, status, ticket["ticket_uuid"]))
    cn.execute("""
        INSERT INTO voc.classification_projection(ticket_uuid,analysis_kind,request_id,value,status)
        VALUES (%s,%s,%s,%s,%s) ON CONFLICT(ticket_uuid,analysis_kind) DO UPDATE
        SET request_id=excluded.request_id,value=excluded.value,status=excluded.status
    """, (ticket["ticket_uuid"], job["analysis_kind"], job["request_id"], str(value) if value is not None else None, status))
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
        SELECT 1 FROM voc.silver_messages WHERE event_id=%(event_id)s AND schema_version=4
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
            SELECT j.* FROM voc.classification_job j JOIN voc.silver_messages s ON s.event_id=j.event_id AND s.schema_version=4
            WHERE j.company_uuid=%s AND j.state IN ('pending','running') AND j.ready_at<=clock_timestamp()
              AND (s.message_kind='customer_inquiry' OR s.body_version<>j.body_version)
            ORDER BY j.ready_at,j.queued_at,j.request_id LIMIT 1 FOR UPDATE OF j SKIP LOCKED
        """, (config.company,)).fetchone()
        if job is None:
            return False, None
        if job["state"] == "running":
            cn.execute("""
                UPDATE voc.classification_attempt SET finished_at=clock_timestamp(),outcome='expired',error_code='lease_expired'
                WHERE request_id=%(request_id)s AND attempt_no=%(attempt_count)s AND finished_at IS NULL
            """, job)
        ticket = current_ticket(cn, job)
        if ticket is None and pause_withdrawn(cn, job):
            return True, None
        if job["attempt_count"] >= MAX_ATTEMPTS:
            applied = project(cn, job, ticket, None, "failed")
            terminate(cn, job, "failed", applied=applied)
            return True, None
        if not project(cn, job, ticket, None, "running"):
            terminate(cn, job, "skipped")
            return True, None
        job["attempt_count"] += 1
        cn.execute("""
            UPDATE voc.classification_job SET state='running', attempt_count=%s,
                ready_at=clock_timestamp()+%s*interval '1 second' WHERE request_id=%s
        """, (job["attempt_count"], LEASE_SECONDS, job["request_id"]))
        cn.execute("""
            INSERT INTO voc.classification_attempt(request_id,attempt_no) VALUES(%s,%s)
        """, (job["request_id"], job["attempt_count"]))
        feature = cn.execute("""
            SELECT model_text FROM voc.model_text_history WHERE company_uuid=%(company_uuid)s AND event_id=%(event_id)s
              AND preprocess_version=%(preprocess_version)s AND body_version=%(body_version)s
        """, job).fetchone()
        job.update(domain_id=str(ticket["domain_uuid"]), model_text=feature["model_text"])
        return True, job


class PredictionError(Exception):
    def __init__(self, code, served_version=None):
        super().__init__(code)
        self.code = code
        self.served_version = served_version


def predict(job):
    request_id = str(job["request_id"])
    body = {"request_id": request_id, "analysis_kind": job["analysis_kind"],
            "model_text": job["model_text"], "domain_id": job["domain_id"]}
    request = Request(job["model_url"] + "/v1/predict", json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise PredictionError("response_too_large")
        result = json.loads(raw)
    except HTTPError as exc:
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


def finish(cn, job, label=None, error=None):
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
                SELECT category_uuid FROM voc.classification_category_map WHERE domain_uuid=%s AND label=%s
            """, (job["domain_id"], label)).fetchone()
            if mapping is None:
                error = PredictionError("unmapped_category", job["model_version"])
            else:
                value = mapping["category_uuid"]
        cn.execute("""
            UPDATE voc.classification_attempt SET finished_at=clock_timestamp(), outcome=%s, error_code=%s, served_version=%s
            WHERE request_id=%s AND attempt_no=%s
        """, ("failed" if error else "succeeded", error.code if error else None,
              error.served_version if error else job["model_version"], job["request_id"], job["attempt_count"]))
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
            finish(cn, job, error=exc)
        else:
            finish(cn, job, label=label)
    return queued or claimed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["migrate", "verify-schema", "run", "once"])
    action = parser.parse_args().command
    if action == "run":
        READY.unlink(missing_ok=True)
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row, connect_timeout=5) as cn:
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
