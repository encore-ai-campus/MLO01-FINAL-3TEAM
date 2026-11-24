"""Record stage boundaries and broker offsets alongside their real processing paths."""
from datetime import datetime, timezone
from uuid import UUID

from confluent_kafka import TIMESTAMP_LOG_APPEND_TIME


def observe(cn, event, stage, start=None, end=None):
    if start is not None or end is not None:
        cn.execute("SELECT voc.observe_stage(%s,%s,%s,%s)", (event, stage, start, end))


def fail(cn, stage, ref, event, reason):
    cn.execute("SELECT voc.fail_stage(%s,%s,%s,%s)", (stage, ref, event, reason))


def recover(cn, stage, event):
    cn.execute("SELECT voc.recover_stage(%s,%s)", (stage, event))


def broker_time(msg):
    kind, millis = msg.timestamp()
    if kind == TIMESTAMP_LOG_APPEND_TIME:
        return datetime.fromtimestamp(millis / 1000, timezone.utc)
    return None


def record_delivery(cn, msg, event, stage, observed_at=None):
    cn.execute("""
        INSERT INTO voc.pipeline_kafka(topic,partition_id,kafka_offset,event_id,stage,recorded_at)
        VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
    """, (msg.topic(), msg.partition(), msg.offset(), event, stage,
          observed_at or broker_time(msg) or datetime.now(timezone.utc)))


def observe_flink(cn, event, trace, *, error=None, complete=False):
    # Unidentifiable validation failures still get Kafka offsets and a failure record.
    if event is None:
        return
    try:
        attempt = UUID(trace["attempt_id"])
        started = datetime.fromisoformat(trace["started_at"])
        finished = datetime.fromisoformat(trace["finished_at"]) if complete else None
        partition, offset = trace["request_partition"], trace["request_offset"]
        if (started.utcoffset() is None or (complete and (finished.utcoffset() is None or finished < started))
                or type(partition) is not int or type(offset) is not int or min(partition, offset) < 0):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        raise ValueError("invalid Flink observation") from None
    saved = cn.execute("""
        INSERT INTO voc.flink_attempt(attempt_id,event_id,started_at,finished_at,request_partition,request_offset,error)
        VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(attempt_id) DO UPDATE SET
        finished_at=excluded.finished_at,error=excluded.error
        WHERE excluded.finished_at IS NOT NULL AND voc.flink_attempt.finished_at IS NULL
    """, (attempt, event, started, finished, partition, offset, error))
    observe(cn, event, "flink", started, finished if not error else None)
    if saved.rowcount and not complete:
        interrupted = cn.execute("""
            UPDATE voc.flink_attempt SET error='task_interrupted'
            WHERE event_id=%s AND request_partition=%s AND request_offset=%s AND started_at<%s
              AND finished_at IS NULL AND error IS NULL RETURNING attempt_id
        """, (event, partition, offset, started))
        for previous in interrupted:
            fail(cn, "flink", str(previous["attempt_id"]), event, "task_interrupted")
    if saved.rowcount and error:
        fail(cn, "flink", str(attempt), event, error)
    elif saved.rowcount and complete:
        recover(cn, "flink", event)


def heartbeat(cn, stage):
    cn.execute("SELECT voc.heartbeat_pipeline(%s)", (stage,))
