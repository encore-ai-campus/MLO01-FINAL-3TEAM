import json
from contextlib import contextmanager

from django.db import connection, transaction

from apps.accounts.services import current_actor
from config.database import fetch_rows, read_snapshot
from config.http import DeskError

from . import action_repository as repo, delivery


def require_conduct(actor, cursor):
    if not actor["can_read"] or not repo.can_conduct(cursor, actor):
        raise DeskError(403, "TICKET_CONDUCT_FORBIDDEN", "티켓 상담 권한이 없습니다.")


@contextmanager
def write_ticket(token, ticket_uuid):
    with transaction.atomic(), connection.cursor() as cursor:
        actor = current_actor(token, cursor)
        require_conduct(actor, cursor)
        ticket = repo.lock_ticket(cursor, actor, ticket_uuid)
        if ticket is None:
            raise DeskError(404, "TICKET_NOT_FOUND", "티켓을 찾을 수 없습니다.")
        yield cursor, actor, ticket


def transfer(token, ticket_uuid, recipient, reason, attached_reply):
    with write_ticket(token, ticket_uuid) as (cursor, actor, ticket):
        if recipient in (actor["staff_user_uuid"], ticket["assignee_uuid"]):
            raise DeskError(422, "INVALID_RECIPIENT", "이관할 새 상담사를 선택해 주세요.")
        cursor.execute(f"""
            SELECT tm.team_uuid FROM staff_user s LEFT JOIN team tm USING (company_uuid,team_uuid)
            WHERE s.staff_user_uuid=%s AND s.company_uuid=%s AND {repo.CONDUCT}
        """, (recipient, actor["company_uuid"]))
        target = cursor.fetchone()
        if target is None:
            raise DeskError(422, "INVALID_RECIPIENT", "이관할 새 상담사를 선택해 주세요.")
        team_uuid = target[0]
        cursor.execute("UPDATE ticket SET assignee_uuid=%s,assigned_team_uuid=%s,updated_at=now() WHERE ticket_uuid=%s",
                       (recipient, team_uuid, ticket_uuid))
        event = repo.add_event(cursor, ticket_uuid, actor, "transfer", memo=reason, attached=attached_reply)
        cursor.execute("INSERT INTO ticket_action_recipient VALUES (%s,%s)", (event, recipient))
    return {"event_id": event, "assignee_uuid": recipient, "assigned_team_uuid": team_uuid}


def request_review(token, ticket_uuid, channel, memo, attachment):
    with write_ticket(token, ticket_uuid) as (cursor, actor, _):
        cursor.execute(f"""
            SELECT s.staff_user_uuid FROM ticket_review_channel_member m
            JOIN staff_user s USING (company_uuid,staff_user_uuid)
            WHERE m.company_uuid=%s AND m.review_channel_uuid=%s AND s.staff_user_uuid<>%s AND {repo.CONDUCT}
        """, (actor["company_uuid"], channel, actor["staff_user_uuid"]))
        recipients = cursor.fetchall()
        if not recipients:
            raise DeskError(422, "INVALID_REVIEW_CHANNEL", "검토를 받을 상담사가 있는 공유 채널을 선택해 주세요.")
        event = repo.add_event(cursor, ticket_uuid, actor, "review", channel=channel, memo=memo,
                               attachment=json.dumps(attachment) if attachment else None)
        cursor.executemany("INSERT INTO ticket_action_recipient VALUES (%s,%s)",
                           [(event, user) for user, in recipients])
    return {"review_request_uuid": event, "recipient_count": len(recipients)}


def received_requests(token, kind=None):
    with read_snapshot() as cursor:
        actor = current_actor(token, cursor)
        require_conduct(actor, cursor)
        return repo.inbox(cursor, actor, kind)


def request_attachment(token, action_uuid):
    with read_snapshot() as cursor:
        actor = current_actor(token, cursor)
        require_conduct(actor, cursor)
        attachment = repo.request_attachment(cursor, actor, action_uuid)
        if attachment is None:
            raise DeskError(404, "ATTACHMENT_NOT_FOUND", "첨부 자료를 찾을 수 없습니다.")
        return attachment


def store_message(token, ticket_uuid, message_type, body, request_key):
    with write_ticket(token, ticket_uuid) as (cursor, actor, ticket):
        cursor.execute(f"SELECT {repo.MESSAGE_FIELDS} FROM ticket_message WHERE ticket_uuid=%s AND request_key=%s",
                       (ticket_uuid, request_key))
        existing = next(iter(fetch_rows(cursor)), None)
        if existing:
            if (existing["staff_user_uuid"], existing["message_type"], existing["body"]) != (
                actor["staff_user_uuid"], message_type, body
            ):
                raise DeskError(409, "REQUEST_KEY_REUSED", "요청 키가 이미 다른 메시지에 사용됐습니다.")
            return {"message": existing, "ticket_status": ticket["status"]}

        source, status = None, None
        if message_type == "reply":
            if ticket["csv_import_kind"] == "migration":
                status = "internal_only"
            else:
                source = repo.reply_source(cursor, ticket_uuid)
                if source is None:
                    raise DeskError(409, "REPLY_ROUTE_UNAVAILABLE", "고객 회신 경로를 확인해 주세요.")
                cursor.execute("""
                    SELECT FROM ticket_message WHERE ticket_uuid=%s AND delivery_status IN ('sending','unknown')
                """, (ticket_uuid,))
                if cursor.fetchone() is not None:
                    raise DeskError(409, "DELIVERY_UNCERTAIN", "기존 답변의 발송 결과를 확인해 주세요.")
                status = "sending"
        cursor.execute(f"""
            INSERT INTO ticket_message
                (ticket_uuid,staff_user_uuid,body,sender_type,message_type,delivery_status,created_at,request_key)
            VALUES (%s,%s,%s,'staff',%s,%s,now(),%s) RETURNING {repo.MESSAGE_FIELDS}
        """, (ticket_uuid, actor["staff_user_uuid"], body, message_type, status, request_key))
        message = fetch_rows(cursor)[0]
        cursor.execute("UPDATE ticket SET updated_at=now() WHERE ticket_uuid=%s", (ticket_uuid,))

    if status != "sending":
        return {"message": message, "ticket_status": ticket["status"]}

    provider_id = None
    try:
        provider_id = delivery.send_reply(source, body)
        status = "sent"
    except delivery.DeliveryError as error:
        status = error.status

    # The unique active-delivery record survives timeouts and prevents an unsafe retry.
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SELECT status FROM ticket WHERE ticket_uuid=%s FOR UPDATE", (ticket_uuid,))
        ticket_status = cursor.fetchone()[0]
        cursor.execute(f"""
            UPDATE ticket_message SET delivery_status=%s,provider_message_id=%s
            WHERE ticket_message_uuid=%s RETURNING {repo.MESSAGE_FIELDS}
        """, (status, provider_id, message["ticket_message_uuid"]))
        message = fetch_rows(cursor)[0]
        if status == "sent" and ticket_status != "답변완료":
            repo.add_event(cursor, ticket_uuid, actor, "reply_completed", from_status=ticket_status, to_status="답변완료")
            ticket_status = "답변완료"
        cursor.execute("UPDATE ticket SET status=%s,updated_at=now() WHERE ticket_uuid=%s", (ticket_status, ticket_uuid))
    return {"message": message, "ticket_status": ticket_status}
