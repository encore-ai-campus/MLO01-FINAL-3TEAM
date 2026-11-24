import json
import os
import time
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from confluent_kafka import Consumer, KafkaError, KafkaException, Producer
from confluent_kafka.admin import AdminClient, AlterConfigOpType, ConfigEntry, ConfigResource, NewTopic, ResourceType

from .events import ROOT, archive_path, atomic_json, event_id

BROKERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
RAW = ("voc.raw.discord", "voc.raw.gmail")
REQUEST = "voc.standardize.request.v1"
STANDARD = "voc.standardized.v1"
REVIEW = "voc.standardize.review.v1"
MAX_BYTES = 64 * 1024 * 1024
COLUMNS = ("schema_version","company_id","channel","connection_id","source_message_id",
           "source_conversation_id","reply_to_message_id","sender_ref","title","body_text",
           "occurred_at","received_at")


def db():
    return psycopg.connect(host="postgres", dbname="voc", user="voc",
                           password=Path("/run/secrets/postgres_password").read_text().strip(),
                           row_factory=dict_row, autocommit=True)


def init():
    with db() as cn:
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


def publish_spool():
    kafka = producer()
    pending = ROOT / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    while True:
        for path in sorted(pending.glob("*.json")):
            event = json.loads(path.read_bytes())
            # Keep the provider JSON independently readable even during a broker outage.
            target = archive_path(ROOT, event)
            atomic_json(target, event["raw"])
            send(kafka, "voc.raw." + event["channel"], event["event_id"], event)
            path.unlink()
        time.sleep(.5)


def store_bronze(cn, topic, event):
    channel = event["channel"]
    fields = [event[k] for k in ("company_id","channel","connection_id","source_message_id")]
    if not all(isinstance(x,str) and x for x in fields) or channel not in ("discord","gmail"):
        raise ValueError("invalid source identity")
    if topic != "voc.raw." + channel or event_id(*fields) != event["event_id"] or not isinstance(event["raw"],dict):
        raise ValueError("invalid envelope")
    values = (event["event_id"], *fields, event["origin"], event["received_at"],
              Jsonb(event["context"]), Jsonb(event["raw"]))
    cn.execute("""WITH saved AS (
        INSERT INTO voc.bronze_messages
          (event_id,company_id,channel,connection_id,source_message_id,origin,received_at,context,raw_payload)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (event_id) DO NOTHING RETURNING event_id)
        INSERT INTO voc.standardize_outbox(event_id) SELECT event_id FROM saved""",values)


def relay(cn, kafka):
    with cn.transaction():
        row = cn.execute("""SELECT b.event_id::text, b.company_id,b.channel,b.connection_id,b.source_message_id,
                     b.origin,b.received_at,b.context,b.raw_payload AS raw
                 FROM voc.standardize_outbox o JOIN voc.bronze_messages b USING(event_id)
                 WHERE o.published_at IS NULL ORDER BY o.queued_at,o.event_id
                 LIMIT 1 FOR UPDATE OF o SKIP LOCKED""").fetchone()
        if not row:
            return False
        row["received_at"] = row["received_at"].isoformat()
        send(kafka, REQUEST, row["event_id"], row)
        cn.execute("UPDATE voc.standardize_outbox SET published_at=now() WHERE event_id=%s",(row["event_id"],))
        return True


def add_review(cn, msg, ident, reason):
    cn.execute("""INSERT INTO voc.review_events(topic,partition_id,kafka_offset,event_id,reason)
                  VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
               (msg.topic(),msg.partition(),msg.offset(),ident,reason))


def store_message(cn, msg):
    ident = None
    try:
        record = json.loads(msg.value())
        if not isinstance(record,dict):
            raise ValueError("invalid event JSON")
        ident = record.get("event_id")
        if not isinstance(ident,str):
            ident = None
        with cn.transaction():
            if msg.topic() in RAW:
                store_bronze(cn,msg.topic(),record)
            elif msg.topic() == REVIEW:
                add_review(cn,msg,ident,record.get("error") or "standardization error")
            else:
                cn.execute("INSERT INTO voc.silver_messages (event_id," + ",".join(COLUMNS)
                           + ") VALUES (" + ",".join(["%s"]*(1+len(COLUMNS))) + ") ON CONFLICT (event_id) DO NOTHING",
                           (record["event_id"], *(record[col] for col in COLUMNS)))
    except (ValueError, KeyError, TypeError, psycopg.DataError, psycopg.IntegrityError) as error:
        # Malformed inputs are durably accounted for before the Kafka offset advances.
        # Never include the raw payload or driver diagnostics containing personal data.
        with cn.transaction():
            add_review(cn,msg,ident,"invalid input: " + type(error).__name__)


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
