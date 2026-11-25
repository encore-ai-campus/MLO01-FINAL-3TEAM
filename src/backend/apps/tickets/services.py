from contextlib import contextmanager

from django.db import connection, transaction

from apps.accounts.services import TICKET_HANDLE, current_actor, require_work_permission
from config.database import read_snapshot
from config.http import DeskError

from . import repository


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
