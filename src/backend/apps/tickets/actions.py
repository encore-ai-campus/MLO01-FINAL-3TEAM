from contextlib import contextmanager

from django.db import connection, transaction

from apps.accounts.services import TICKET_HANDLE, current_actor, require_work_permission
from apps.assignment.services import refill_after_completion
from config.database import fetch_rows, read_snapshot
from config.http import DeskError

from . import action_repository as repo, delivery


def require_conduct(actor, cursor):
    if not actor["can_read"]:
        raise DeskError(403, "TICKET_READ_FORBIDDEN", "티켓 조회 권한이 없습니다.")
    require_work_permission(cursor, actor, TICKET_HANDLE)


def require_assignee(actor, ticket):
    if ticket["assignee_uuid"] != actor["staff_user_uuid"]:
        raise DeskError(403, "TICKET_ASSIGNEE_REQUIRED", "현재 담당 상담사만 이 작업을 할 수 있습니다.")


@contextmanager
def write_ticket(token, ticket_uuid):
    with transaction.atomic(), connection.cursor() as cursor:
        actor = current_actor(token, cursor)
        require_conduct(actor, cursor)
        ticket = repo.lock_ticket(cursor, actor, ticket_uuid)
        if ticket is None:
            raise DeskError(404, "TICKET_NOT_FOUND", "티켓을 찾을 수 없습니다.")
        yield cursor, actor, ticket


def complete_ticket(token, ticket_uuid):
    with write_ticket(token, ticket_uuid) as (cursor, actor, ticket):
        require_assignee(actor, ticket)
        if ticket["status"] == "답변완료":
            return
        cursor.execute("""
            INSERT INTO event_type(code) VALUES ('ticket_manually_completed') ON CONFLICT (code) DO NOTHING
        """)
        repo.add_event(cursor, ticket_uuid, actor, "ticket_manually_completed",
                       from_status=ticket["status"], to_status="답변완료")
        cursor.execute("UPDATE ticket SET status='답변완료',updated_at=now() WHERE ticket_uuid=%s", (ticket_uuid,))
        transaction.on_commit(lambda: refill_after_completion(ticket_uuid), robust=True)


def transfer(token, ticket_uuid, recipient, reason, attached_reply):
    with write_ticket(token, ticket_uuid) as (cursor, actor, ticket):
        require_assignee(actor, ticket)
        if recipient == actor["staff_user_uuid"]:
            raise DeskError(422, "INVALID_RECIPIENT", "이관할 새 상담사를 선택해 주세요.")
        cursor.execute(f"""
            SELECT FROM staff_user s
            WHERE s.staff_user_uuid=%s AND s.company_uuid=%s AND {repo.CONDUCT}
        """, (recipient, actor["company_uuid"]))
        if cursor.fetchone() is None:
            raise DeskError(422, "INVALID_RECIPIENT", "이관할 새 상담사를 선택해 주세요.")
        cursor.execute("""
            SELECT 1 FROM collab_request WHERE ticket_uuid=%s AND requester_uuid=%s
              AND request_type='transfer' AND status='pending' LIMIT 1
        """, (ticket_uuid, actor["staff_user_uuid"]))
        if cursor.fetchone() is not None:
            raise DeskError(409, "TRANSFER_PENDING", "이미 수락을 기다리는 이관 요청이 있습니다.")
        request = repo.add_request(cursor, ticket_uuid, actor, recipient, "transfer", "pending", reason)
        if attached_reply:
            repo.add_attachment(cursor, request, {"content_type": "text/plain", "content": attached_reply})
    return {"transfer_request_uuid": request, "status": "pending"}


def accept_transfer(token, ticket_uuid, request_uuid):
    with write_ticket(token, ticket_uuid) as (cursor, actor, ticket):
        recipient = actor["staff_user_uuid"]
        cursor.execute("""
            SELECT requester_uuid,status,memo FROM collab_request
            WHERE ticket_uuid=%s AND collab_request_uuid=%s AND recipient_uuid=%s AND request_type='transfer'
            FOR UPDATE
        """, (ticket_uuid, request_uuid, recipient))
        request = cursor.fetchone()
        if request is None:
            raise DeskError(404, "TRANSFER_NOT_FOUND", "이관 요청을 찾을 수 없습니다.")
        requester, status, reason = request
        if status == "accepted":
            return
        if status != "pending" or requester != ticket["assignee_uuid"]:
            raise DeskError(409, "TRANSFER_NOT_AVAILABLE", "담당자가 변경됐거나 이미 처리된 요청입니다.")
        if repo.has_unsettled_reply(cursor, ticket_uuid):
            raise DeskError(409, "DELIVERY_UNCERTAIN", "기존 답변의 발송 결과를 확인한 뒤 이관을 수락해 주세요.")
        cursor.execute("""
            UPDATE ticket SET assignee_uuid=%s,assigned_team_uuid=%s,updated_at=now() WHERE ticket_uuid=%s
        """, (recipient, actor["team_uuid"], ticket_uuid))
        cursor.execute("""
            UPDATE collab_request SET status='accepted',decided_at=now(),read_at=coalesce(read_at, now())
            WHERE collab_request_uuid=%s
        """, (request_uuid,))
        repo.add_event(cursor, ticket_uuid, actor, "ticket_transfer", request=request_uuid,
                       recipient=recipient, reason=reason)


def request_review(token, ticket_uuid, channel, memo, attachment):
    with write_ticket(token, ticket_uuid) as (cursor, actor, _):
        cursor.execute(f"""
            SELECT s.staff_user_uuid FROM ticket_review_channel_member m
            JOIN staff_user s USING (company_uuid,staff_user_uuid)
            WHERE m.company_uuid=%s AND m.review_channel_uuid=%s AND s.staff_user_uuid<>%s AND {repo.CONDUCT}
            ORDER BY s.staff_user_uuid
        """, (actor["company_uuid"], channel, actor["staff_user_uuid"]))
        recipients = cursor.fetchall()
        if not recipients:
            raise DeskError(422, "INVALID_REVIEW_CHANNEL", "검토를 받을 상담사가 있는 공유 채널을 선택해 주세요.")
        requests = []
        for recipient, in recipients:
            request = repo.add_request(cursor, ticket_uuid, actor, recipient, "review", "pending", memo, channel)
            if attachment:
                repo.add_attachment(cursor, request, attachment)
            repo.add_event(cursor, ticket_uuid, actor, "ticket_review_requested", request=request, recipient=recipient)
            requests.append(request)
    return {"review_request_uuid": requests[0], "recipient_count": len(requests)}


def received_requests(token, kind=None):
    with read_snapshot() as cursor:
        actor = current_actor(token, cursor)
        require_conduct(actor, cursor)
        return repo.inbox(cursor, actor, kind)


def request_attachment(token, action_uuid):
    with read_snapshot() as cursor:
        actor = current_actor(token, cursor)
        if not actor["can_read"]:
            raise DeskError(403, "TICKET_READ_FORBIDDEN", "티켓 조회 권한이 없습니다.")
        attachment = repo.request_attachment(cursor, actor, action_uuid)
        if attachment is None:
            raise DeskError(404, "ATTACHMENT_NOT_FOUND", "첨부 자료를 찾을 수 없습니다.")
        return attachment


def store_message(token, ticket_uuid, message_type, body, request_key):
    with write_ticket(token, ticket_uuid) as (cursor, actor, ticket):
        if message_type == "reply":
            require_assignee(actor, ticket)
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
                if repo.has_unsettled_reply(cursor, ticket_uuid):
                    raise DeskError(409, "DELIVERY_UNCERTAIN", "기존 답변의 발송 결과를 확인해 주세요.")
                status = "sending"
        cursor.execute(f"""
            INSERT INTO ticket_message
                (ticket_uuid,staff_user_uuid,body,sender_type,message_type,delivery_status,created_at,request_key,
                 reviewed_feature_uuid)
            VALUES (%s,%s,%s,'staff',%s,%s,now(),%s,
                    CASE WHEN %s THEN voc.ticket_feature_uuid(%s) END) RETURNING {repo.MESSAGE_FIELDS}
        """, (ticket_uuid, actor["staff_user_uuid"], body, message_type, status, request_key,
              status == "sending", ticket_uuid))
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
            UPDATE ticket_message SET delivery_status=%s,provider_message_id=%s,
                sent_at=CASE WHEN %s THEN coalesce(sent_at,clock_timestamp()) ELSE sent_at END
            WHERE ticket_message_uuid=%s RETURNING {repo.MESSAGE_FIELDS}
        """, (status, provider_id, status == "sent", message["ticket_message_uuid"]))
        message = fetch_rows(cursor)[0]
        if status == "sent" and ticket_status != "답변완료":
            repo.add_event(cursor, ticket_uuid, actor, "ticket_reply_completed",
                           from_status=ticket_status, to_status="답변완료")
            ticket_status = "답변완료"
            transaction.on_commit(lambda: refill_after_completion(ticket_uuid), robust=True)
        cursor.execute("UPDATE ticket SET status=%s,updated_at=now() WHERE ticket_uuid=%s", (ticket_status, ticket_uuid))
    return {"message": message, "ticket_status": ticket_status}
