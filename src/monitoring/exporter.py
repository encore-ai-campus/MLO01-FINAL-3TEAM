"""Read collector status, Kafka group offsets and Flink REST metadata."""
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from confluent_kafka import Consumer, KafkaException, TopicPartition

ROOT = Path("/data")
BROKERS = "kafka:9092"
FLINK = "http://jobmanager:8081"
GROUP_TOPICS = {
    "encore-flink-standardize-v4": ("voc.standardize.request.v4",),
    "encore-bronze-silver-v1": ("voc.raw.discord", "voc.raw.gmail", "voc.standardized.v4", "voc.standardize.review.v4"),
}
SOURCES = ("gmail", "discord")


def sample(rows, name, value, **labels):
    encoded = ",".join(f"{key}={json.dumps(str(item), ensure_ascii=False)}" for key, item in labels.items())
    rows.append(f"voc_{name}" + ("{" + encoded + "}" if labels else "") + f" {value}")


def capture_metrics(rows):
    for source in SOURCES:
        try:
            info = json.loads((ROOT / "status" / f"{source}.json").read_text())
            observed = datetime.fromisoformat(info["at"])
            if observed.tzinfo is None:
                continue
            ready = int(info["state"] == "ready")
            sample(rows, "collector_status_observed_seconds", observed.timestamp(), channel=source)
            sample(rows, "collector_ready", ready, channel=source)
        except (OSError, KeyError, ValueError, TypeError):
            continue
    path = ROOT / "capture.sqlite3"
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as cn:
            pending = dict(cn.execute("SELECT channel,count(*) FROM capture_outbox WHERE published_at IS NULL GROUP BY channel"))
            cursors = cn.execute("SELECT company_id,channel,connection_id,scope_id,position,phase FROM collector_checkpoint")
            for company, channel, connection, scope, position, phase in cursors:
                opaque = hashlib.sha256(json.dumps([company, connection, scope]).encode()).hexdigest()[:12]
                sample(rows, "collector_position", 1, channel=channel, source=opaque,
                       position=position if position.isascii() and position.isdecimal() else "비공개",
                       phase=phase if phase in {"bootstrap", "active"} else "미확인")
            for source in SOURCES:
                sample(rows, "capture_pending", pending.get(source, 0), channel=source)
    except sqlite3.Error:
        return


def kafka_metrics(rows):
    try:
        for group, topics in GROUP_TOPICS.items():
            consumer = Consumer({"bootstrap.servers": BROKERS, "group.id": group, "enable.auto.commit": False,
                                 "socket.timeout.ms": 1500, "log_level": 0})
            try:
                metadata = consumer.list_topics(timeout=1.5)
                partitions = [TopicPartition(topic, part) for topic in topics if topic in metadata.topics
                              for part in metadata.topics[topic].partitions]
                if not partitions:
                    continue
                for entry in consumer.committed(partitions, timeout=1.5):
                    if entry.error:
                        continue
                    low, high = consumer.get_watermark_offsets(TopicPartition(entry.topic, entry.partition), timeout=1)
                    labels = {"group": group, "topic": entry.topic, "partition": entry.partition}
                    sample(rows, "kafka_high_watermark", high, **labels)
                    if entry.offset >= 0:
                        sample(rows, "kafka_committed_offset", entry.offset, **labels)
                        sample(rows, "kafka_consumer_lag", max(0, high - max(low, entry.offset)), **labels)
            finally:
                consumer.close()
    except KafkaException:
        return False
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
    capture_metrics(rows)
    sample(rows, "observation_available", int(kafka_metrics(rows)), metric="kafka")
    sample(rows, "observation_available", int(flink_metrics(rows)), metric="flink")
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


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9108), MetricsHandler).serve_forever()
