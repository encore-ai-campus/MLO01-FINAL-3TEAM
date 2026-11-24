from contextlib import contextmanager
from uuid import UUID, uuid4

from django.core import signing
from django.db import connection, transaction

from django.db import connection, transaction

from apps.accounts.services import TICKET_HANDLE, current_actor, require_work_permission
from config.database import read_snapshot
from config.http import DeskError

from . import repository
from .presentation.forms import SENTIMENTS

DISPLAY_SALT = "ticket-predictions-displayed"


def add_memo(token, ticket_uuid, body):
    with transaction.atomic(), connection.cursor() as cursor:
        actor = current_actor(token, cursor)
        if not actor["can_read"]:
            raise DeskError(403, "TICKET_READ_FORBIDDEN", "티켓 조회 권한이 없습니다.")
        require_work_permission(cursor, actor, TICKET_HANDLE)
        memo = repository.add_memo(cursor, actor["company_uuid"], actor["staff_user_uuid"], ticket_uuid, body)
        if memo is None:
            raise DeskError(404, "TICKET_NOT_FOUND", "티켓을 찾을 수 없습니다.")
        return memo


class TicketDesk:
    """Authorized ticket reads share the employee and visibility snapshot."""

    def __init__(self, cursor, actor):
        self._cursor = cursor
        self.actor = actor

    def list_tickets(self, query):
        if query.days is not None and query.days not in (1, 3, 7, 30):
            raise DeskError(422, "INVALID_PERIOD", "최근 기간은 1·3·7·30일 중에서 선택해 주세요.")
        if query.start is not None and not 0 <= (query.end - query.start).days < 365:
            raise DeskError(422, "INVALID_PERIOD", "시작일과 종료일을 포함해 최대 365일까지 조회할 수 있습니다.")
        assignee = self.actor["staff_user_uuid"] if query.scope == "mine" else None
        return repository.list_tickets(self._cursor, self.actor["company_uuid"], assignee, query)

    def filter_options(self):
        return repository.filter_options(self._cursor, self.actor["company_uuid"])

    def detail(self, ticket_uuid):
        result = repository.detail(self._cursor, self.actor["company_uuid"], ticket_uuid)
        if result is None:
            raise DeskError(404, "TICKET_NOT_FOUND", "티켓을 찾을 수 없습니다.")
        return result


@contextmanager
def open_desk(token):
    if not token or len(token) > 128:
        raise DeskError(401, "LOGIN_REQUIRED", "로그인해 주세요.")
    with read_snapshot() as cursor:
        actor = current_actor(token, cursor)
        if not actor["can_read"]:
            raise DeskError(403, "TICKET_READ_FORBIDDEN", "티켓 조회 권한이 없습니다.")
        yield TicketDesk(cursor, actor)


def display_token(desk, tickets):
    rows = repository.displayed_predictions(desk._cursor, tickets) if tickets else []
    if rows:
        return signing.dumps({"staff": str(desk.actor["staff_user_uuid"]), "predictions": rows}, salt=DISPLAY_SALT)
    return None


def record_display(token, signed, displayed):
    try:
        snapshot = signing.loads(signed, max_age=600, salt=DISPLAY_SALT)
    except signing.BadSignature:
        raise DeskError(422, "INVALID_DISPLAY", "페이지를 새로 고친 뒤 다시 시도해 주세요.") from None
    with transaction.atomic(), connection.cursor() as cursor:
        actor = current_actor(token, cursor)
        if not actor["can_read"] or str(actor["staff_user_uuid"]) != snapshot["staff"]:
            raise DeskError(403, "TICKET_READ_FORBIDDEN", "티켓 조회 권한이 없습니다.")
        for row in snapshot["predictions"]:
            if row["ticket_uuid"] + ":" + row["kind"] in displayed:
                repository.record_display(cursor, actor, row)


def reply_options(desk, ticket):
    from .reply import pending_reply, reply_endpoint

    if ticket["status"] == "답변완료" or not repository.can_reply(desk._cursor, desk.actor):
        return None
    pending = pending_reply(desk._cursor, ticket["ticket_uuid"])
    own = pending and str(pending["staff_user_uuid"]) == str(desk.actor["staff_user_uuid"])
    return {
        "sentiments": SENTIMENTS, "categories": desk.filter_options()["categories"],
        "deliveries": [] if pending else repository.confirmed_replies(desk._cursor, desk.actor, ticket["ticket_uuid"]),
        "connected": bool(reply_endpoint()), "blocked": pending and not own, "pending": own,
        "reply_id": pending["reply_uuid"] if own else uuid4(), "body": pending["body"] if own else "",
    }


def lock_reply_ticket(cursor, token, ticket):
    actor = current_actor(token, cursor)
    if not actor["can_read"] or not repository.can_reply(cursor, actor):
        raise DeskError(403, "TICKET_REPLY_FORBIDDEN", "티켓 답변 권한이 없습니다.")
    locked = repository.lock_visible(cursor, actor, ticket)
    if locked is None:
        raise DeskError(404, "TICKET_NOT_FOUND", "티켓을 찾을 수 없습니다.")
    return actor, locked


def update_ticket(token, ticket, data, *, complete=False):
    with transaction.atomic(), connection.cursor() as cursor:
        actor, locked = lock_reply_ticket(cursor, token, ticket)
        try:
            if complete:
                from .reply import pending_reply

                if pending_reply(cursor, ticket):
                    raise DeskError(409, "REPLY_IN_PROGRESS", "발송 확인을 기다리고 있는 답변이 있습니다.")
                delivery = UUID(data["delivery"])
                if locked["status"] == "답변완료" and str(locked["reply_delivery_uuid"]) == str(delivery):
                    return
                if not repository.complete_delivered(cursor, actor, ticket, delivery):
                    raise DeskError(409, "DELIVERY_UNCONFIRMED", "발송이 확인된 답변을 선택해 주세요.")
                return
            if locked["status"] == "답변완료":
                raise DeskError(409, "TICKET_COMPLETED", "답변완료한 티켓입니다.")
            sentiment = data["sentiment"] or None
            category = UUID(data["category"]) if data["category"] else None
            if sentiment is not None and sentiment not in SENTIMENTS:
                raise ValueError
            if not repository.set_classification(cursor, ticket, locked["domain_uuid"], sentiment, category):
                raise ValueError
        except (KeyError, ValueError):
            raise DeskError(422, "INVALID_CLASSIFICATION", "감성과 카테고리를 확인해 주세요.") from None
