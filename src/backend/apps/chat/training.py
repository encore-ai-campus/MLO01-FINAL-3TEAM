"""Store training labels from a chat service's authenticated completion event."""
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone as datetime_timezone
from uuid import UUID

from django.db import connection, transaction
from django.utils import timezone

from apps.accounts.services import current_actor
from apps.tickets.presentation.forms import SENTIMENTS
from apps.tickets.repository import category_exists
from config.http import DeskError


@dataclass(frozen=True)
class ChatInput:
    snapshot_uuid: UUID
    version: int
    sha256: str


@dataclass(frozen=True)
class ChatPrediction:
    attempt_uuid: UUID
    input: ChatInput
    value: str
    traffic: str
    model_name: str
    model_version: str
    rollout: str
    ended_at: datetime


@dataclass(frozen=True)
class ChatDisplay:
    staff_uuid: UUID
    attempt_uuid: UUID
    value: str
    displayed_at: datetime


@dataclass(frozen=True)
class ChatEdit:
    staff_uuid: UUID
    input: ChatInput
    value: str
    edited_at: datetime


@dataclass(frozen=True)
class ChatLabel:
    value: str | None
    prediction: ChatPrediction | None = None
    display: ChatDisplay | None = None
    edit: ChatEdit | None = None


@dataclass(frozen=True)
class ChatClosed:
    completion_uuid: UUID
    conversation_uuid: UUID
    company_uuid: UUID
    domain_uuid: UUID
    completed_by: UUID
    completed_at: datetime
    input: ChatInput
    labels: dict[str, ChatLabel]


def eligible_prediction(event, label):
    prediction = label.prediction
    if (prediction and prediction.traffic == "operational" and prediction.input == event.input and prediction.value
            and isinstance(prediction.attempt_uuid, UUID) and prediction.model_name.strip()
            and prediction.model_version.strip() and prediction.rollout in ("stable", "canary")
            and timezone.is_aware(prediction.ended_at) and prediction.ended_at <= event.completed_at):
        return prediction
    return None


def label_evidence(event, label, finisher, prediction):
    seen, edit = label.display, label.edit
    if (prediction and seen and seen.staff_uuid == finisher and seen.attempt_uuid == prediction.attempt_uuid
            and seen.value == prediction.value == label.value and timezone.is_aware(seen.displayed_at)
            and prediction.ended_at <= seen.displayed_at <= event.completed_at):
        return "상담사 확인 후 유지", seen.staff_uuid, seen.displayed_at
    if (edit and edit.value == label.value and edit.input == event.input
            and isinstance(edit.staff_uuid, UUID) and timezone.is_aware(edit.edited_at)
            and edit.edited_at <= event.completed_at):
        return "상담사 직접 수정·지정", edit.staff_uuid, edit.edited_at
    return None


def valid_label(cursor, domain, kind, value):
    if kind == "sentiment":
        return value in SENTIMENTS
    try:
        category = UUID(value)
    except ValueError:
        return False
    return str(category) == value and category_exists(cursor, domain, category)


def encode_event_value(value):
    if isinstance(value, datetime) and timezone.is_aware(value):
        return value.astimezone(datetime_timezone.utc).isoformat()
    return str(value)


def record_chat_end(token, event: ChatClosed):
    """Call on the server with the saved close event and evidence from the same chat."""
    snapshot = event.input
    if (not all(isinstance(key, UUID) for key in (
            event.completion_uuid, event.conversation_uuid, event.company_uuid, event.domain_uuid, event.completed_by,
            snapshot.snapshot_uuid))
            or type(snapshot.version) is not int or not 0 < snapshot.version <= 2147483647
            or not re.fullmatch(r"[0-9a-f]{64}", snapshot.sha256)
            or not timezone.is_aware(event.completed_at) or event.completed_at > timezone.now()
            or not event.labels.keys() <= {"sentiment", "category"}):
        raise DeskError(422, "INVALID_CHAT_COMPLETION", "채팅 종료 기록과 문의 버전을 확인해 주세요.")
    payload = json.dumps(asdict(event), sort_keys=True, ensure_ascii=False, default=encode_event_value)
    checksum = hashlib.sha256(payload.encode()).hexdigest()
    with transaction.atomic(), connection.cursor() as cursor:
        actor = current_actor(token, cursor)
        company, finisher = actor["company_uuid"], actor["staff_user_uuid"]
        if event.company_uuid != company or event.completed_by != finisher:
            raise DeskError(403, "CHAT_COMPLETION_FORBIDDEN", "채팅 종료 권한이 없습니다.")
        cursor.execute("SELECT domain_uuid FROM public.company WHERE company_uuid=%s", (company,))
        if cursor.fetchone()[0] != event.domain_uuid:
            raise DeskError(403, "INVALID_CHAT_DOMAIN", "소속 회사의 도메인을 확인해 주세요.")
        cursor.execute("""
            INSERT INTO voc.chat_training_completion(
                completion_uuid,conversation_uuid,company_uuid,domain_uuid,completed_by,completed_at,
                input_uuid,input_version,input_sha256,event_sha256
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(completion_uuid) DO NOTHING
        """, (event.completion_uuid, event.conversation_uuid, company, event.domain_uuid, finisher,
              event.completed_at, snapshot.snapshot_uuid, snapshot.version, snapshot.sha256, checksum))
        if not cursor.rowcount:
            cursor.execute("""
                SELECT company_uuid,completed_by,event_sha256 FROM voc.chat_training_completion
                WHERE completion_uuid=%s
            """, (event.completion_uuid,))
            if cursor.fetchone() != (company, finisher, checksum):
                raise DeskError(409, "CHAT_COMPLETION_CONFLICT", "저장된 채팅 종료 기록과 내용이 다릅니다.")
            return False

        for kind, label in event.labels.items():
            if label.value is None or not label.value.strip():
                continue
            if not valid_label(cursor, event.domain_uuid, kind, label.value):
                raise DeskError(422, "INVALID_CHAT_LABEL", "감성과 카테고리를 확인해 주세요.")
            prediction = eligible_prediction(event, label)
            if prediction and not valid_label(cursor, event.domain_uuid, kind, prediction.value):
                prediction = None
            evidence = label_evidence(event, label, finisher, prediction)
            if evidence is None:
                continue
            origin, label_staff, evidence_at = evidence
            cursor.execute("""
                SELECT FROM public.staff_user WHERE company_uuid=%s AND staff_user_uuid=%s
            """, (company, label_staff))
            if cursor.fetchone() is None:
                raise DeskError(403, "INVALID_CHAT_LABEL_STAFF", "분류를 지정한 상담사의 소속을 확인해 주세요.")
            cursor.execute("""
                INSERT INTO voc.retraining_candidate(
                    chat_completion_uuid,channel,company_uuid,domain_uuid,input_uuid,input_version,
                    kind,value,origin,completed_by,completed_at,label_staff_uuid,evidence_at,
                    chat_attempt_uuid,predicted_value,model_name,model_version,rollout
                ) VALUES (%s,'chat',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (event.completion_uuid, company, event.domain_uuid, snapshot.snapshot_uuid, snapshot.version,
                  kind, label.value, origin, finisher, event.completed_at, label_staff, evidence_at,
                  prediction.attempt_uuid if prediction else None, prediction.value if prediction else None,
                  prediction.model_name if prediction else None, prediction.model_version if prediction else None,
                  prediction.rollout if prediction else None))
        return True
