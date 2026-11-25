"""Run with python3 src/collector/test_silver_v4_integration.py [--v1-schema path/to/previous/schema.sql]."""

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import timezone
from pathlib import Path


RECEIVED = "2026-11-24T01:02:03.456789Z"
MAILBOX = "help@example.test"
BODY = "배송 상태를 확인해 주세요."
FIELDS = set("""
    schema_version event_id company_id channel connection_id source_message_id source_conversation_id
    source_channel_id source_channel_name reply_to_message_id message_kind sender_ref sender_display_name
    title body_text body_version occurred_at received_at source_metadata
""".split())


def run_isolated(legacy_schema=None):
    root = Path(__file__).resolve().parents[2]
    project = "voc-silver-qa-" + uuid.uuid4().hex[:12]
    compose = ["docker", "compose", "--env-file", os.devnull, "-p", project]
    services = ("postgres", "kafka", "init-db-topics", "db-worker", "jobmanager", "taskmanager")
    config = json.loads(subprocess.check_output(
        [*compose, "-f", str(root / "compose.yaml"), "config", "--format", "json", *services],
        env={**os.environ, "DISCORD_CHANNEL_ID": "1"},
    ))
    for key in list(config):
        if key.startswith("x-"):
            del config[key]
    config["services"] = {name: config["services"][name] for name in services}
    config["networks"] = {"default": {}}
    config["volumes"] = {key: {} for key in ("pg_data", "kafka_data", "flink_checkpoints")}
    with tempfile.TemporaryDirectory(prefix=project) as folder:
        password = Path(folder) / "pg-password"
        password.write_text(uuid.uuid4().hex)
        config["secrets"] = {"postgres_password": {"file": str(password)}}
        for name, service in config["services"].items():
            service.pop("ports", None)
            service["restart"] = "no"
            if name in ("db-worker", "init-db-topics"):
                service["image"] = project + "-worker"
            if name in ("jobmanager", "taskmanager"):
                service["image"] = project + "-flink"
            if name in ("init-db-topics", "taskmanager"):
                service.pop("build")
        config["services"]["db-worker"].setdefault("environment", {})["SILVER_QA_PROJECT"] = project
        manifest = Path(folder) / "compose.json"
        manifest.write_text(json.dumps(config))
        command = [*compose, "-f", str(manifest)]
        try:
            subprocess.run([*command, "build", "db-worker", "jobmanager"], check=True)
            script = Path(__file__).read_bytes()
            if legacy_schema is not None:
                subprocess.run([*command, "up", "-d", "--wait", "postgres", "kafka"], check=True)
                subprocess.run([*command, "exec", "-T", "postgres", "psql", "-X", "-v", "ON_ERROR_STOP=1",
                                "-U", "voc", "-d", "voc"], input=legacy_schema.read_bytes(), check=True)
                subprocess.run([*command, "run", "--rm", "--no-deps", "-T", "--entrypoint", "python",
                                "db-worker", "-", "--seed-v1"], input=script, check=True)
            subprocess.run([*command, "up", "-d"], check=True)
            test_mode = "--check-migration" if legacy_schema is not None else "--inside"
            subprocess.run([*command, "exec", "-T", "db-worker", "python", "-", test_mode],
                           input=script, check=True, timeout=360)
        except (subprocess.SubprocessError, KeyboardInterrupt):
            subprocess.run([*command, "ps", "-a"], check=False)
            subprocess.run([*command, "logs", "--tail", "25", "db-worker", "jobmanager", "init-db-topics"],
                           check=False)
            raise
        finally:
            subprocess.run([*command, "down", "-v", "--timeout", "2"], check=True)
            subprocess.run(["docker", "image", "rm", project + "-worker", project + "-flink"],
                           check=False)


def discord_event(message_id, parent=None):
    from collector.events import make_event

    message = {"id": message_id, "channel_id": "444", "guild_id": "555", "type": 19 if parent else 0,
               "content": BODY, "author": {"id": "777", "global_name": "Global", "username": "qa-user"},
               "member": {"nick": "시험 고객"}, "timestamp": "2026-11-24T10:02:00.012+09:00"}
    if parent:
        message["message_reference"] = {"message_id": parent}
    return make_event("discord", "555", message_id, "discord_gateway",
                      {"op": 0, "t": "MESSAGE_CREATE", "d": message}, is_thread=False, channel_name="문의")


def gmail_event(message_id, labels, *, minimal=False):
    from collector.events import make_event

    encoded_name = base64.b64encode("메일 고객".encode()).decode()
    headers = [{"name": "From", "value": f"=?UTF-8?B?{encoded_name}?= <buyer@example.test>"}]
    if not minimal:
        headers.extend([{"name": "Subject", "value": "배송 문의"},
                        {"name": "To", "value": f'"QA, Support" <{MAILBOX}>, archive@example.test'}])
    raw = {"id": message_id, "threadId": "same-thread", "labelIds": labels,
           "payload": {"mimeType": "text/plain", "headers": headers,
                       "body": {"data": base64.urlsafe_b64encode(BODY.encode()).decode()}}}
    if not minimal:
        raw["internalDate"] = "1001"
    return make_event("gmail", MAILBOX, message_id, "gmail_api", raw, label="VOC")


def wait_for(description, check, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.15)
    raise AssertionError("Timed out waiting for " + description)


def seed_v1():
    from psycopg.types.json import Jsonb
    from collector.pipeline import db

    event = gmail_event("ff", ["VOC"])
    with db() as cn, cn.transaction():
        cn.execute("""INSERT INTO voc.bronze_messages(event_id,company_id,channel,connection_id,source_message_id,
                         origin,received_at,context,raw_payload) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                   (event["event_id"], event["company_id"], "gmail", MAILBOX, "ff", event["origin"], RECEIVED,
                    Jsonb(event["context"]), Jsonb(event["raw"])))
        cn.execute("INSERT INTO voc.standardize_outbox(event_id, published_at) VALUES (%s,%s)",
                   (event["event_id"], RECEIVED))
        cn.execute("""INSERT INTO voc.silver_messages(event_id, schema_version, company_id,channel,connection_id,
                         source_message_id,source_conversation_id,title,body_text,received_at,classification_status)
                      VALUES (%s,1,%s,'gmail',%s,'ff','same-thread','','v1 saved text',%s,'reviewed')""",
                   (event["event_id"], event["company_id"], MAILBOX, RECEIVED))
        cn.execute("""INSERT INTO voc.review_events(topic,partition_id,kafka_offset,event_id,reason)
                      VALUES ('voc.standardize.review.v1',0,1,%s,'legacy review')""", (event["event_id"],))


def check_automatic_intake(cn, submit, outputs, wait_worker_offsets):
    """Plain inbound messages become tickets through Kafka and Flink on enabled routes."""
    from collector.events import COMPANY

    with cn.transaction():
        domain = cn.execute("INSERT INTO public.domain(name) VALUES ('QA') RETURNING domain_uuid").fetchone()
        company = cn.execute("""INSERT INTO public.company(name,email_domain,domain_uuid)
                                VALUES ('Route QA','route-qa.example.test',%s) RETURNING company_uuid""",
                             (domain["domain_uuid"],)).fetchone()["company_uuid"]
        for channel, connection, room in (("discord", "555", "444"), ("gmail", MAILBOX, "")):
            destination = cn.execute("""INSERT INTO public.channel(company_uuid,name,type)
                                        VALUES (%s,%s,%s) RETURNING channel_uuid""",
                                     (company, channel, channel)).fetchone()["channel_uuid"]
            cn.execute("""INSERT INTO voc.ticket_source_connection
                              (company_id,channel,connection_id,source_channel_id,company_uuid,channel_uuid,auto_customer_from)
                          VALUES (%s,%s,%s,%s,%s,%s,clock_timestamp())""",
                       (COMPANY, channel, connection, room, company, destination))

    discord = discord_event("991")
    discord["context"]["channel_id"] = "444"
    gmail = gmail_event("b991", ["INBOX", "Label_42"])
    events = (discord, gmail)
    ids = [event["event_id"] for event in events]
    for event in events:
        submit(event)
    confirmed = outputs(set(ids), kind="customer_inquiry")
    assert all(record["body_text"] == BODY for _, record in confirmed.values())
    wait_worker_offsets("voc.standardized.v4")
    for event in events:
        submit(event)  # A redelivery must reuse the same ticket and two analysis jobs.
    wait_worker_offsets("voc.raw.discord", "voc.raw.gmail")
    counts = cn.execute("""SELECT
          (SELECT count(*) FROM public.visible_ticket WHERE company_uuid=%s AND source_event_id=ANY(%s::uuid[])) AS tickets,
          (SELECT count(*) FROM voc.ticket_analysis_outbox WHERE company_uuid=%s AND event_id=ANY(%s::uuid[])) AS jobs""",
                        (company, ids, company, ids)).fetchone()
    assert counts == {"tickets": 2, "jobs": 4}, counts
    print("PASS: ordinary Discord and labeled Gmail inputs -> Flink -> two visible tickets and four jobs.", flush=True)


def check_pipeline(migrated):
    from confluent_kafka import Consumer, TopicPartition
    from collector.pipeline import BROKERS, REVIEW, STANDARD, db, init, producer, send

    assert (STANDARD, REVIEW) == ("voc.standardized.v4", "voc.standardize.review.v4")
    consumer = Consumer({"bootstrap.servers": BROKERS, "group.id": "silver-v4-qa",
                         "auto.offset.reset": "earliest", "enable.auto.commit": False})
    kafka = producer()
    consumer.subscribe([STANDARD, REVIEW])
    parent = discord_event("101")
    child = discord_event("102", parent["source_message_id"])
    first = gmail_event("a1", ["VOC", "INBOX"])
    second = gmail_event("a2", [], minimal=True)
    sent = gmail_event("a3", ["SENT"])
    invalid = gmail_event("a4", ["VOC"])
    invalid["raw"]["payload"]["body"]["data"] = "!invalid-base64!"
    events = (child, first, second, sent, invalid)

    def submit(event):
        send(kafka, "voc.raw." + event["channel"], event["event_id"], event)

    def wait_worker_offsets(*topics):
        partitions = [TopicPartition(topic, 0) for topic in topics]
        ends = {p.topic: consumer.get_watermark_offsets(p, timeout=10)[1] for p in partitions}
        # Query committed offsets without joining the DB worker's group.
        reader = Consumer({"bootstrap.servers": BROKERS, "group.id": "encore-bronze-silver-v1",
                           "enable.auto.commit": False})
        try:
            wait_for("DB worker commits for " + ", ".join(topics), lambda: all(
                p.offset >= ends[p.topic] for p in reader.committed(partitions, timeout=10)))
        finally:
            reader.close()

    def outputs(expected_ids, kind=None):
        found = {}

        def read():
            msg = consumer.poll(.3)
            if msg is not None:
                if msg.error():
                    raise RuntimeError(msg.error())
                record = json.loads(msg.value())
                key = record["event_id"]
                if key in expected_ids and (kind is None or record.get("message_kind") == kind):
                    found[key] = (msg.topic(), record)
            return found if found.keys() == expected_ids else None

        return wait_for("Kafka v4 results " + str(expected_ids), read)

    def check_output(event, record, **extra):
        expected = {k: event[k] for k in ("event_id", "company_id", "channel", "connection_id", "source_message_id")}
        expected.update(schema_version=4, body_version=1, received_at=RECEIVED, body_text=BODY,
                        message_kind="unknown", reply_to_message_id=None, title=None)
        expected.update(extra)
        assert set(record) == FIELDS and type(record["body_version"]) is int and type(record["schema_version"]) is int
        assert record == expected, (record, expected)

    try:
        with db() as cn:
            def silver(event):
                return cn.execute("SELECT * FROM voc.silver_messages WHERE event_id=%s AND schema_version=4",
                                  (event["event_id"],)).fetchone()

            def reviews(event):
                return {row["reason"] for row in cn.execute("SELECT reason FROM voc.review_events WHERE event_id=%s",
                                                           (event["event_id"],))}

            def raw_reviews(event):
                rows = cn.execute("SELECT reason FROM voc.review_events WHERE event_id=%s AND topic=%s",
                                  (event["event_id"], "voc.raw." + event["channel"]))
                return [row["reason"] for row in rows]

            def compare_storage(event, record):
                row = wait_for("Silver row " + event["source_message_id"], lambda: silver(event))
                stored = {k: row[k] for k in FIELDS}
                stored["event_id"] = str(stored["event_id"])
                for name in ("received_at", "occurred_at"):
                    if stored[name] is not None:
                        stored[name] = stored[name].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                assert stored == record, (stored, record)
                return row["standardized_at"]

            if migrated:
                legacy = gmail_event("ff", ["VOC"])
                ident = legacy["event_id"]
                backfilled = wait_for("v1 Bronze backfill", lambda: silver(legacy))
                archived = cn.execute("SELECT * FROM voc.silver_messages_v1 WHERE event_id=%s", (ident,)).fetchone()
                assert archived["body_text"] == "v1 saved text" and archived["classification_status"] == "reviewed"
                assert archived["received_at"] == backfilled["received_at"] and backfilled["body_text"] == BODY
                assert cn.execute("SELECT published_at FROM voc.standardize_outbox_v1 WHERE event_id=%s",
                                  (ident,)).fetchone()["published_at"] == backfilled["received_at"]
                assert cn.execute("SELECT schema_version FROM voc.review_events WHERE event_id=%s AND reason=%s",
                                  (ident, "legacy review")).fetchone()["schema_version"] == 1
                published = cn.execute("SELECT published_at FROM voc.standardize_outbox WHERE event_id=%s",
                                       (ident,)).fetchone()["published_at"]
                init()
                assert cn.execute("SELECT published_at FROM voc.standardize_outbox WHERE event_id=%s",
                                  (ident,)).fetchone()["published_at"] == published and published is not None
                print("PASS: v1 archive, v4 backfill and repeated initialization.", flush=True)

            for event in (parent, *events):
                event["received_at"] = RECEIVED
            for event in events:
                submit(event)
                if event is first:
                    submit({**first, "received_at": "2026-11-25T12:00:00.000000Z",
                            "raw": {**first["raw"], "labelIds": ["VOC", "INBOX", "UNREAD"], "historyId": "200"}})
                if event is child:
                    submit({**child, "origin": "discord_history", "raw": child["raw"]["d"]})

            records = outputs({event["event_id"] for event in events})
            docs = {key: doc for key, (topic, doc) in records.items() if topic == STANDARD}
            child_doc = docs[child["event_id"]]
            check_output(child, child_doc, source_channel_id="444", source_channel_name="문의",
                         source_conversation_id=None, reply_to_message_id="101", sender_ref="777",
                         sender_display_name="시험 고객", occurred_at="2026-11-24T01:02:00.012000Z", source_metadata={})
            for event in (first, second, sent):
                minimal = event is second
                check_output(event, docs[event["event_id"]], source_channel_id=None, source_channel_name=None,
                             source_conversation_id="same-thread", sender_ref="buyer@example.test",
                             sender_display_name="메일 고객", title=None if minimal else "배송 문의",
                             occurred_at=None if minimal else "1970-01-01T00:00:01.001000Z",
                             message_kind="staff_reply" if event is sent else "unknown",
                             source_metadata={"label_ids": event["raw"]["labelIds"],
                                              "to_addresses": [] if minimal else [MAILBOX, "archive@example.test"]})
            review_topic, rejected = records[invalid["event_id"]]
            assert review_topic == REVIEW and set(rejected) == {"event_id", "schema_version", "error"}
            assert rejected["schema_version"] == 4 and rejected["error"] == "invalid MIME text"
            wait_for("durable MIME review", lambda: rejected["error"] in reviews(invalid))
            assert silver(invalid) is None
            for event in (child, first, second, sent):
                compare_storage(event, docs[event["event_id"]])

            wait_worker_offsets("voc.raw.discord", "voc.raw.gmail")
            assert raw_reviews(child) == [] and raw_reviews(first) == []
            edited_discord = {**child, "origin": "discord_history", "raw": {**child["raw"]["d"], "content": "수정본"}}
            edited_gmail = gmail_event(first["source_message_id"], first["raw"]["labelIds"])
            edited_gmail["raw"]["payload"]["body"]["data"] = base64.urlsafe_b64encode("수정본".encode()).decode()
            for changed in (edited_discord, edited_gmail):
                submit(changed)
                submit(changed)
            wait_worker_offsets("voc.raw.discord", "voc.raw.gmail")
            for event in (child, first):
                assert raw_reviews(event) == ["source body differs from first Bronze record"]
                raw = cn.execute("SELECT origin, raw_payload, received_at FROM voc.bronze_messages WHERE event_id=%s",
                                 (event["event_id"],)).fetchone()
                assert raw["origin"] == event["origin"] and raw["raw_payload"] == event["raw"]
                assert raw["received_at"] == silver(event)["received_at"] and silver(event)["body_text"] == BODY
            initial_count = len(events) + int(migrated)
            assert cn.execute("SELECT count(*) AS n FROM voc.bronze_messages").fetchone()["n"] == initial_count
            assert cn.execute("SELECT count(*) AS n FROM voc.standardize_outbox").fetchone()["n"] == initial_count

            original = docs[first["event_id"]]
            timestamp = silver(first)["standardized_at"]
            previous_reviews = reviews(first)
            send(kafka, STANDARD, first["event_id"], original)
            send(kafka, STANDARD, first["event_id"], {**original, "body_text": "동일 버전 본문 변경"})
            wait_for("same-version body conflict review", lambda: reviews(first) - previous_reviews)
            assert compare_storage(first, original) == timestamp

            submit(parent)
            wait_for("late Discord parent", lambda: silver(child)["source_conversation_id"] == "101")
            assert silver(parent)["source_conversation_id"] == "101"
            for event in (first, second):
                cn.execute("""UPDATE voc.standardize_outbox SET verified_message_kind='customer_inquiry'
                              WHERE event_id=%s AND target_schema_version=4""", (event["event_id"],))
            confirmed = outputs({first["event_id"], second["event_id"]}, kind="customer_inquiry")
            for event in (first, second):
                topic, record = confirmed[event["event_id"]]
                assert topic == STANDARD and record == {**docs[event["event_id"]], "message_kind": "customer_inquiry"}
                wait_for("confirmed customer " + event["source_message_id"],
                         lambda: silver(event)["message_kind"] == "customer_inquiry")
            cn.execute("""UPDATE voc.standardize_outbox SET verified_message_kind=NULL
                          WHERE event_id=%s AND target_schema_version=4""", (first["event_id"],))
            released = outputs({first["event_id"]}, kind="unknown")
            assert released[first["event_id"]] == (STANDARD, original)
            wait_for("withdrawn role confirmation", lambda: silver(first)["message_kind"] == "unknown")
            send(kafka, STANDARD, first["event_id"], confirmed[first["event_id"]][1])
            wait_worker_offsets(STANDARD)
            assert silver(first)["message_kind"] == "unknown" and silver(second)["message_kind"] == "customer_inquiry"
            assert cn.execute("SELECT count(*) AS n FROM voc.silver_messages WHERE schema_version=4").fetchone()["n"] == initial_count
            check_automatic_intake(cn, submit, outputs, wait_worker_offsets)
        print("PASS: 19-field Kafka/SQL parity, distinct Gmail messages, role confirmation and withdrawal, "
              "late Discord parent, raw envelope and body replay, and Silver conflict review.", flush=True)
    finally:
        consumer.close()


if __name__ == "__main__":
    if sys.argv[1:] in (["--seed-v1"], ["--inside"], ["--check-migration"]):
        if not os.environ.get("SILVER_QA_PROJECT", "").startswith("voc-silver-qa-"):
            sys.exit("Container validation requires the isolated QA stack.")
        if sys.argv[1] == "--seed-v1":
            seed_v1()
        else:
            check_pipeline(sys.argv[1] == "--check-migration")
    elif not sys.argv[1:]:
        run_isolated()
    elif len(sys.argv) == 3 and sys.argv[1] == "--v1-schema":
        run_isolated(Path(sys.argv[2]))
    else:
        sys.exit(__doc__)
