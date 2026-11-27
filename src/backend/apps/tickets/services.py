from contextlib import contextmanager
from http.client import HTTPException

from django.conf import settings
from django.db import connection, transaction

from apps.accounts.services import TICKET_HANDLE, current_actor, require_work_permission
from config.database import read_snapshot
from config.http import DeskError

from . import action_repository, repository, summary


def add_memo(token, ticket_id, body):
    with transaction.atomic(), connection.cursor() as cursor:
        actor = current_actor(token, cursor)
        if not actor["can_read"]:
            raise DeskError(403, "TICKET_READ_FORBIDDEN", "티켓 조회 권한이 없습니다.")
        require_work_permission(cursor, actor, TICKET_HANDLE)
        ticket = action_repository.lock_ticket(cursor, actor, ticket_id)
        if ticket is None:
            raise DeskError(404, "TICKET_NOT_FOUND", "티켓을 찾을 수 없습니다.")
        if ticket["status"] == "문의 종료":
            raise DeskError(409, "TICKET_CLOSED", "종료된 문의는 처리할 수 없습니다.")
        memo = repository.add_memo(cursor, actor["company_id"], actor["staff_user_id"], ticket_id, body)
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
        assignee = self.actor["staff_user_id"] if query.scope == "mine" else None
        return repository.list_tickets(self._cursor, self.actor["company_id"], assignee, query)

    def message_attachment(self, ticket_id, attachment_id):
        can_conduct = action_repository.can_conduct(self._cursor, self.actor)
        return action_repository.visible_message_attachment(
            self._cursor, self.actor, ticket_id, attachment_id, can_conduct,
        )

    def filter_options(self):
        return repository.filter_options(self._cursor, self.actor["company_id"])

    def summary_source(self, ticket_id):
        require_work_permission(self._cursor, self.actor, TICKET_HANDLE)
        source = repository.summary_source(self._cursor, self.actor["company_id"], ticket_id)
        if source is None:
            raise DeskError(404, "TICKET_NOT_FOUND", "티켓을 찾을 수 없습니다.")
        if source["status"] == "문의 종료":
            raise DeskError(409, "TICKET_CLOSED", "종료된 문의는 요약할 수 없습니다.")
        if not (source["body"] or "").strip():
            raise DeskError(422, "TICKET_BODY_EMPTY", "요약할 고객 문의 본문이 없습니다.")
        return source

    def detail(self, ticket_id, with_controls=False):
        can_conduct = action_repository.can_conduct(self._cursor, self.actor)
        result = repository.detail(
            self._cursor, self.actor["company_id"], ticket_id, can_conduct, for_page=with_controls,
        )
        if result is None:
            raise DeskError(404, "TICKET_NOT_FOUND", "티켓을 찾을 수 없습니다.")
        result["can_conduct"] = can_conduct
        if with_controls and can_conduct:
            result.update(action_repository.options(self._cursor, self.actor, result["ticket"]))
            result["quick_replies"] = action_repository.quick_replies(self._cursor, self.actor, ticket_id)
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


def summarize_ticket(token, ticket_id):
    with open_desk(token) as desk:
        source = desk.summary_source(ticket_id)
    # Release the read transaction before waiting for inference.
    try:
        return summary.summarize_inquiry(source["body"], source["channel"], settings.VOC_OLLAMA_URL)
    except summary.EmptyInquiry:
        raise DeskError(422, "SUMMARY_QUOTED_ONLY", "인용된 메일을 제외하면 요약할 본문이 없습니다.") from None
    except (OSError, HTTPException):
        raise DeskError(503, "SUMMARY_UNAVAILABLE", "요약 모델에 연결할 수 없습니다.") from None
    except ValueError:
        raise DeskError(502, "SUMMARY_INVALID", "모델이 유효한 한 줄 요약을 만들지 못했습니다.") from None
