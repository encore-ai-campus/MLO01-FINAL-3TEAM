"""Expose actual SQLite cursor, Kafka lag and Flink recovery observations."""
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

import psycopg
from confluent_kafka import Consumer, KafkaException, TopicPartition

from .events import ROOT
from .inference import endpoint
from .observations import heartbeat
from .pipeline import BROKERS, DB_GROUP, FLINK_ENTER, FLINK_GROUP, RAW, REQUEST, REVIEW, STANDARD, db

FLINK = os.getenv("VOC_FLINK_REST_URL", "http://jobmanager:8081")
SOURCES = ("gmail", "discord")


def sample(rows, name, value, **labels):
    encoded = ",".join(f"{key}={json.dumps(str(item), ensure_ascii=False)}" for key, item in labels.items())
    rows.append(f"voc_{name}" + ("{" + encoded + "}" if labels else "") + f" {value}")


def capture_metrics(rows):
    for source in SOURCES:
        ready = 0
        try:
            info = json.loads((ROOT / "status" / f"{source}.json").read_text())
            observed = datetime.fromisoformat(info["at"])
            ready = int(info["state"] == "ready" and
                        (datetime.now(timezone.utc) - observed).total_seconds() < 150)
            sample(rows, "collector_status_observed_seconds", observed.timestamp(), channel=source)
            sample(rows, "collector_ready", ready, channel=source)
        except (OSError, KeyError, ValueError):
            continue
    path = ROOT / "capture.sqlite3"
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1) as cn:
            stats = dict(cn.execute("SELECT channel,count(*) FROM capture_outbox WHERE published_at IS NULL GROUP BY channel"))
            columns = {col[1] for col in cn.execute("PRAGMA table_info(collector_checkpoint)")}
            cursor_rows = cn.execute("""
                SELECT company_id,channel,connection_id,scope_id,position,phase,""" +
                                     ("updated_at" if "updated_at" in columns else "NULL") +
                                     " FROM collector_checkpoint").fetchall()
            issues = cn.execute("SELECT channel,reason,count(*) FROM capture_issue GROUP BY channel,reason").fetchall()
    except sqlite3.Error:
        return []
    for source in SOURCES:
        sample(rows, "capture_pending", stats.get(source, 0), channel=source)
    positions = []
    for company, channel, connection, scope, position, phase, updated in cursor_rows:
        opaque = hashlib.sha256(json.dumps([company, connection, scope]).encode()).hexdigest()[:12]
        labels = {"channel": channel, "source": opaque}
        positions.append((channel, opaque, position if position.isdecimal() else "비공개", phase, updated))
        if updated:
            sample(rows, "collector_cursor_updated_seconds", datetime.fromisoformat(updated).timestamp(), **labels)
    for channel, reason, count in issues:
        sample(rows, "capture_issues", count, channel=channel, reason=reason)
    return positions


def kafka_metrics(rows, progress):
    for group, topics in ((FLINK_GROUP, (REQUEST,)), (DB_GROUP, (*RAW, STANDARD, REVIEW, FLINK_ENTER))):
        with_consumer = Consumer({"bootstrap.servers": BROKERS, "group.id": group, "enable.auto.commit": False,
                                  "socket.timeout.ms": 1500, "log_level": 0})
        try:
            metadata = with_consumer.list_topics(timeout=1.5)
            partitions = [TopicPartition(topic, part) for topic in topics if topic in metadata.topics
                          for part in metadata.topics[topic].partitions]
            for entry in with_consumer.committed(partitions, timeout=1.5):
                if entry.error:
                    continue
                low, high = with_consumer.get_watermark_offsets(TopicPartition(entry.topic, entry.partition), timeout=1.5)
                labels = {"group": group, "topic": entry.topic, "partition": entry.partition}
                sample(rows, "kafka_high_watermark", high, **labels)
                progress.append((group,entry.topic,entry.partition,entry.offset if entry.offset>=0 else None,high))
                if entry.offset >= 0:
                    sample(rows, "kafka_committed_offset", entry.offset, **labels)
                    sample(rows, "kafka_consumer_lag", max(0, high - max(low, entry.offset)), **labels)
                else:
                    sample(rows, "kafka_consumer_lag", max(0, high - low), **labels)
        except KafkaException:
            return False
        finally:
            with_consumer.close()
    return True


def flink_json(path):
    with urlopen(FLINK + path, timeout=2) as response:
        return json.load(response)


def flink_metrics(rows):
    try:
        jobs = flink_json("/jobs/overview")["jobs"]
        jobs = [job for job in jobs if job["name"] == "Encore VOC standardization v4"]
        if not jobs:
            return True
        job = max(jobs, key=lambda item: item["start-time"])
        sample(rows, "flink_job_state", 1, state=job["state"])
        checkpoints = flink_json("/jobs/" + job["jid"] + "/checkpoints")
        for status in ("completed", "failed", "restored"):
            sample(rows, "flink_checkpoints", checkpoints["counts"][status], state=status)
        if completed := checkpoints["latest"].get("completed"):
            stamp = completed.get("latest_ack_timestamp", 0)
            if stamp > 0:
                sample(rows, "flink_last_checkpoint_seconds", stamp / 1000)
                sample(rows, "flink_last_checkpoint_duration_ms", completed["end_to_end_duration"])
        if restored := checkpoints["latest"].get("restored"):
            sample(rows, "flink_last_restore_seconds", restored["restore_timestamp"] / 1000)
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return True


def scrape():
    rows = []
    positions = capture_metrics(rows)
    progress = []
    runtime = os.getenv("VOC_MONITOR_RUNTIME", "true").lower() == "true"
    sample(rows, "observation_available", int(runtime and kafka_metrics(rows, progress)), metric="kafka")
    sample(rows, "observation_available", int(runtime and flink_metrics(rows)), metric="flink")
    live = []
    if any(row.startswith('voc_flink_job_state{state="RUNNING"}') for row in rows):
        live.append("flink")
    if any(row.startswith("voc_collector_ready") and row.endswith(" 1") for row in rows):
        live.append("capture")
    if runtime:
        try:
            readiness = Request("http://ticket-desk:8000/monitoring/readiness", headers={"Host": "localhost"})
            with urlopen(readiness, timeout=1) as response:
                if json.load(response).get("reply_connected") is True:
                    live.append("answer_completion")
        except (OSError, ValueError, AttributeError):
            pass
    if live or positions or progress:
        try:
            with db() as cn, cn.transaction(), cn.cursor() as cursor:
                for stage in live:
                    heartbeat(cn, stage)
                cursor.executemany("""
                    INSERT INTO voc.kafka_progress(consumer_group,topic,partition_id,committed_offset,high_watermark,observed_at)
                    VALUES (%s,%s,%s,%s,%s,clock_timestamp())
                    ON CONFLICT(consumer_group,topic,partition_id) DO UPDATE SET
                        committed_offset=excluded.committed_offset,high_watermark=excluded.high_watermark,
                        observed_at=excluded.observed_at
                """, progress)
                cursor.executemany("""
                    INSERT INTO voc.collector_position
                        (channel,connection_hash,position,phase,updated_at,observed_at)
                    VALUES (%s,%s,%s,%s,%s,clock_timestamp())
                    ON CONFLICT(channel,connection_hash) DO UPDATE SET position=excluded.position,phase=excluded.phase,
                        updated_at=excluded.updated_at,observed_at=excluded.observed_at
                    WHERE voc.collector_position.updated_at IS NULL OR excluded.updated_at>=voc.collector_position.updated_at
                """, positions)
        except (OSError, psycopg.Error):
            pass
    for api in ("classification", "embedding"):
        sample(rows, "model_configured", int(bool(endpoint(api))), api=api)
    return "\n".join(rows) + "\n"


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        content = scrape().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *_):
        pass


def serve_metrics():
    ThreadingHTTPServer(("0.0.0.0", 9108), MetricsHandler).serve_forever()
