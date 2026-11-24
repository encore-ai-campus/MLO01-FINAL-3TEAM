import json
import os
import time
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from confluent_kafka import Consumer, KafkaError, KafkaException, Producer
from confluent_kafka.admin import AdminClient, AlterConfigOpType, ConfigEntry, ConfigResource, NewTopic, ResourceType

from .events import ROOT, archive_path, atomic_json, event_id, source_body_changed
from .spool import CaptureStore
from .silver import REQUEST_ROWS, VERSION, SilverError, known_root, store_silver

BROKERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
RAW = ("voc.raw.discord", "voc.raw.gmail")
REQUEST = "voc.standardize.request.v4"
STANDARD = "voc.standardized.v4"
REVIEW = "voc.standardize.review.v4"
MAX_BYTES = 64 * 1024 * 1024


def db():
    return psycopg.connect(host="postgres", dbname="voc", user="voc",
                           password=Path("/run/secrets/postgres_password").read_text().strip(),
                           row_factory=dict_row, autocommit=True)


def init():
    with db() as cn, cn.transaction():
        cn.execute(Path(__file__).with_name("schema.sql").read_text())
    admin = AdminClient({"bootstrap.servers": BROKERS})
    topics = (*RAW, REQUEST, STANDARD, REVIEW)
    jobs = admin.create_topics([NewTopic(topic, 1, 1, config={"max.message.bytes": str(MAX_BYTES)})
                                for topic in topics])
    existing = []
    for topic, job in jobs.items():
        try:
            job.result()
        except KafkaException as exc:
            if exc.args[0].code() != KafkaError.TOPIC_ALREADY_EXISTS:
                raise
            limit = ConfigEntry("max.message.bytes", str(MAX_BYTES), incremental_operation=AlterConfigOpType.SET)
            existing.append(ConfigResource(ResourceType.TOPIC, topic, incremental_configs=[limit]))
    if existing:
        for change in admin.incremental_alter_configs(existing).values():
            change.result()
    print("PostgreSQL schema and Kafka topics ready", flush=True)


def producer():
    return Producer({"bootstrap.servers":BROKERS, "enable.idempotence":True, "acks":"all",
                     "message.max.bytes":MAX_BYTES, "delivery.timeout.ms":20000})


def send(kafka, topic, key, value):
    delivered = []
    kafka.produce(topic, key=key, value=json.dumps(value,ensure_ascii=False,separators=(",",":")).encode(),
                  on_delivery=lambda error, _: delivered.append(error))
    kafka.flush(25)
    if not delivered or delivered[0]:
        raise RuntimeError(f"Kafka delivery failed: {topic}, {delivered}")


def migrate_capture():
    store = CaptureStore(ROOT)
    count = 0
    with db() as cn, cn.transaction(), cn.cursor(name="capture_migration") as cursor:
        cursor.execute("""SELECT event_id, company_id, channel, connection_id, source_message_id,
                                 received_at FROM voc.bronze_messages""")
        while rows := cursor.fetchmany(500):
            store.confirm_bronze(rows)
            count += len(rows)
    print(f"기존 Bronze 수신 확인 {count}건", flush=True)


def publish_next(store, root, kafka):
    event = store.pending()
    if event is None:
        return False
    atomic_json(archive_path(root, event), event["raw"])
    send(kafka, "voc.raw." + event["channel"], event["event_id"], event)
    store.published(event["event_id"])
    return True


def publish_spool():
    store = CaptureStore(ROOT)
    kafka = producer()
    while True:
        if not publish_next(store, ROOT, kafka):
            time.sleep(.5)


def store_bronze(cn, topic, event):
    channel = event["channel"]
    fields = [event[k] for k in ("company_id","channel","connection_id","source_message_id")]
    if not all(isinstance(x,str) and x for x in fields) or channel not in ("discord","gmail"):
        raise ValueError("invalid source identity")
    if (topic != "voc.raw." + channel or event_id(*fields) != event["event_id"]
            or not isinstance(event["raw"], dict) or not isinstance(event["context"], dict)
            or not isinstance(event["origin"], str) or not isinstance(event["received_at"], str)):
        raise ValueError("invalid envelope")
    values = (event["event_id"], *fields, event["origin"], event["received_at"],
              Jsonb(event["context"]), Jsonb(event["raw"]))
    saved = cn.execute("""WITH saved AS (
        INSERT INTO voc.bronze_messages
          (event_id,company_id,channel,connection_id,source_message_id,origin,received_at,context,raw_payload)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (event_id) DO NOTHING RETURNING event_id)
        INSERT INTO voc.standardize_outbox(event_id, target_schema_version) SELECT event_id, 4 FROM saved""",values)
    if not saved.rowcount:
        first = cn.execute("SELECT raw_payload FROM voc.bronze_messages WHERE event_id=%s", (event["event_id"],)).fetchone()
        if source_body_changed(channel, first["raw_payload"], event["raw"]):
            raise SilverError("source body differs from first Bronze record")


def relay(cn, kafka):
    with cn.transaction():
        row = cn.execute(REQUEST_ROWS + """ AND o.published_at IS NULL ORDER BY o.queued_at,o.event_id
                                            LIMIT 1 FOR UPDATE OF o SKIP LOCKED""").fetchone()
        if not row:
            return False
        row["known_conversation_root"] = known_root(cn, row)
        row["received_at"] = row["received_at"].isoformat()
        send(kafka, REQUEST, row["event_id"], row)
        cn.execute("UPDATE voc.standardize_outbox SET published_at=now() WHERE event_id=%s AND target_schema_version=%s",
                   (row["event_id"], VERSION))
        return True


def add_review(cn, msg, ident, reason):
    cn.execute("""INSERT INTO voc.review_events(topic,partition_id,kafka_offset,event_id,reason,schema_version)
                  VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
               (msg.topic(),msg.partition(),msg.offset(),ident,reason,VERSION))


def store_message(cn, msg):
    ident = None
    try:
        record = json.loads(msg.value())
        if not isinstance(record,dict):
            raise ValueError("invalid event JSON")
        candidate = record.get("event_id")
        if isinstance(candidate, str) and len(candidate) == 36:
            ident = str(UUID(candidate))
        with cn.transaction():
            if msg.topic() in RAW:
                store_bronze(cn,msg.topic(),record)
            elif msg.topic() == REVIEW:
                reason = record.get("error")
                if (record.keys() != {"event_id", "schema_version", "error"} or record["schema_version"] != VERSION
                        or not isinstance(reason, str) or not 0 < len(reason) <= 96):
                    raise SilverError("invalid v4 review record")
                add_review(cn, msg, ident, reason)
            elif msg.topic() == STANDARD:
                store_silver(cn, record)
            else:
                raise ValueError("unexpected topic")
    except (ValueError, KeyError, TypeError, psycopg.DataError, psycopg.IntegrityError) as error:
        # Malformed inputs are durably accounted for before the Kafka offset advances.
        # Never include the raw payload or driver diagnostics containing personal data.
        with cn.transaction():
            reason = str(error) if isinstance(error, SilverError) else "invalid input: " + type(error).__name__
            add_review(cn,msg,ident,reason)


def run_db():
    consumer = Consumer({"bootstrap.servers":BROKERS,"group.id":"encore-bronze-silver-v1",
                         "auto.offset.reset":"earliest","enable.auto.commit":False,
                         "fetch.max.bytes":MAX_BYTES, "max.partition.fetch.bytes":MAX_BYTES})
    consumer.subscribe([*RAW, STANDARD, REVIEW])
    kafka = producer()
    try:
        with db() as cn:
            while True:
                msg = consumer.poll(.25)
                if msg:
                    if msg.error():
                        raise KafkaException(msg.error())
                    store_message(cn,msg)
                    consumer.commit(message=msg,asynchronous=False)
                relay(cn,kafka)
    finally:
        consumer.close()
