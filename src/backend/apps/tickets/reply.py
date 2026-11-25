"""Confirm one reply through the configured provider adapter before completing its ticket."""
import json
import os
from http.client import HTTPException
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID, uuid4

from django.db import connection, transaction

from config.database import fetch_rows
from config.http import DeskError
from . import repository, services

MAX_BODY = 4000


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def reply_endpoint():
    value = os.getenv("VOC_REPLY_URL", "")
    try:
        uri = urlsplit(value)
    except ValueError:
        return ""
    return value if uri.scheme in ("http", "https") and uri.hostname and not uri.username and not uri.password else ""


def pending_reply(cursor, ticket):
    cursor.execute("""
        SELECT reply_uuid,staff_user_uuid,body FROM voc.reply_dispatch
        WHERE ticket_uuid=%s AND delivered_at IS NULL
    """, (ticket,))
    return next(iter(fetch_rows(cursor)), None)


def sent_to_provider(url, reply_id, payload):
    request = Request(url, data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
                      headers={"Content-Type": "application/json", "Idempotency-Key": str(reply_id)})
    if token := os.getenv("VOC_REPLY_TOKEN"):
        request.add_header("Authorization", "Bearer " + token)
    try:
        with build_opener(NoRedirect).open(request, timeout=10) as response:
            raw = response.read(8193)
            if len(raw) > 8192:
                return None
            data = json.loads(raw)
            provider_id = data["provider_delivery_id"]
            if (data["delivery_confirmed"] is True and isinstance(provider_id, str)
                    and 0 < len(provider_id.strip()) <= 128):
                return provider_id
    except (URLError, OSError, HTTPException, ValueError, KeyError, TypeError):
        pass
    return None


def send(token, ticket, data):
    url = reply_endpoint()
    if not url:
        raise DeskError(503, "REPLY_UNAVAILABLE", "회신 API가 연결되지 않았습니다.")
    try:
        reply_id = UUID(data["reply_id"])
        body = data["body"].strip()
        if not 0 < len(body) <= MAX_BODY:
            raise ValueError
    except (KeyError, ValueError):
        raise DeskError(422, "INVALID_REPLY", "답변을 1~4,000자로 입력해 주세요.") from None
    claim = uuid4()
    with transaction.atomic(), connection.cursor() as cursor:
        actor, locked = services.lock_reply_ticket(cursor, token, ticket)
        cursor.execute("""SELECT ticket_uuid,staff_user_uuid,body,delivered_at,
                          lease_until>clock_timestamp() AS busy FROM voc.reply_dispatch WHERE reply_uuid=%s""",
                       (reply_id,))
        dispatch = next(iter(fetch_rows(cursor)), None)
        if dispatch:
            if (str(dispatch["ticket_uuid"]), str(dispatch["staff_user_uuid"]), dispatch["body"]) != (
                    str(ticket), str(actor["staff_user_uuid"]), body):
                raise DeskError(409, "REPLY_CONFLICT", "답변을 작성한 상담사의 같은 내용으로 다시 시도해 주세요.")
            if dispatch["delivered_at"]:
                return
        if locked["status"] == "답변완료":
            raise DeskError(409, "TICKET_COMPLETED", "답변완료한 티켓입니다.")
        if (dispatch and dispatch["busy"]) or (not dispatch and pending_reply(cursor, ticket)):
            raise DeskError(409, "REPLY_IN_PROGRESS", "발송 확인을 기다리고 있는 답변이 있습니다.")
        services.record_form_display(cursor, actor, ticket, data)
        cursor.execute("""
            INSERT INTO voc.reply_dispatch(reply_uuid,ticket_uuid,staff_user_uuid,body,claim_token,lease_until)
            VALUES (%s,%s,%s,%s,%s,clock_timestamp()+interval '30 seconds')
            ON CONFLICT(reply_uuid) DO UPDATE SET claim_token=excluded.claim_token,lease_until=excluded.lease_until
        """, (reply_id, ticket, actor["staff_user_uuid"], body, claim))
        cursor.execute("""
            SELECT b.channel,b.connection_id,b.source_message_id,s.source_channel_id,s.source_conversation_id
            FROM public.ticket t JOIN voc.bronze_messages b ON b.event_id=t.source_event_id
            JOIN voc.silver_messages s ON s.event_id=b.event_id AND s.schema_version=4 WHERE t.ticket_uuid=%s
        """, (ticket,))
        source = fetch_rows(cursor)[0]
    provider_id = sent_to_provider(url, reply_id, {
        **source, "ticket_id": str(ticket), "staff_id": str(actor["staff_user_uuid"]), "body": body,
    })
    with transaction.atomic(), connection.cursor() as cursor:
        if not provider_id:
            cursor.execute("UPDATE voc.reply_dispatch SET claim_token=NULL,lease_until=NULL WHERE claim_token=%s", (claim,))
        else:
            cursor.execute("SELECT 1 FROM ticket WHERE ticket_uuid=%s FOR UPDATE", (ticket,))
            cursor.execute("""
                UPDATE voc.reply_dispatch SET provider_delivery_id=%s,delivered_at=clock_timestamp(),
                       claim_token=NULL,lease_until=NULL
                WHERE reply_uuid=%s AND claim_token=%s RETURNING delivered_at
            """, (provider_id, reply_id, claim))
            completed = cursor.fetchone()
            if completed:
                cursor.execute("""
                    INSERT INTO ticket_message(ticket_message_uuid,ticket_uuid,staff_user_uuid,body,
                                               sender_type,message_type,delivery_status,created_at,sent_at)
                    VALUES (%s,%s,%s,%s,'staff','reply','delivered',%s,%s)
                """, (reply_id, ticket, actor["staff_user_uuid"], body, completed[0], completed[0]))
                if not repository.complete_delivered(cursor, actor, ticket, reply_id):
                    raise DeskError(409, "REPLY_CONFLICT", "티켓의 답변 기록을 다시 확인해 주세요.")
                return
    raise DeskError(502, "DELIVERY_UNCONFIRMED", "발송을 확인하지 못했습니다. 티켓에서 같은 답변을 다시 확인해 주세요.")
