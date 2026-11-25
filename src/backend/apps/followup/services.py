from contextlib import contextmanager

from django.db import connection, transaction

from apps.accounts.repository import company_employee, work_permissions
from apps.accounts.services import CHAT_HANDLE, current_actor
from config.http import DeskError
from . import repository


class FollowUpDesk:
    def __init__(self, cursor, actor):
        self.cursor = cursor
        self.actor = actor
        self.company = actor["company_uuid"]
        self.staff = actor["staff_user_uuid"]
        self.can_read = actor["can_read"]
        self.can_chat = CHAT_HANDLE in work_permissions(cursor, self.company, self.staff, (CHAT_HANDLE,))

    def require_ticket_read(self):
        if not self.can_read:
            raise DeskError(403, "TICKET_READ_FORBIDDEN", "티켓 조회 권한이 없습니다.")

    def track(self, ticket):
        self.require_ticket_read()
        if not repository.source_exists(self.cursor, self.company, ticket, None):
            raise DeskError(404, "TICKET_NOT_FOUND", "티켓을 찾을 수 없습니다.")
        return repository.add_track(self.cursor, self.company, self.staff, ticket)

    def untrack(self, ticket):
        self.require_ticket_read()
        repository.remove_track(self.cursor, self.company, self.staff, ticket)

    def tracks(self):
        self.require_ticket_read()
        return repository.tracks(self.cursor, self.company, self.staff)

    def notifications(self, *, summary=False):
        self.require_ticket_read()
        return repository.notifications(self.cursor, self.company, self.staff, summary=summary)

    def track_options(self):
        self.require_ticket_read()
        return repository.track_options(self.cursor, self.company, self.staff)

    def track_and_schedule(self, data):
        if not self.track(data["ticket_uuid"]):
            raise DeskError(409, "ALREADY_TRACKED", "이미 추적 중인 티켓입니다.")
        return self.schedule(data, reminders=True)

    def schedule(self, data, *, reminders=False):
        if data["chat_uuid"]:
            self.require_chat()
        else:
            self.require_ticket_read()
        if not repository.source_exists(self.cursor, self.company, data["ticket_uuid"], data["chat_uuid"]):
            raise DeskError(404, "SOURCE_NOT_FOUND", "원본 문의를 찾을 수 없습니다.")
        if not company_employee(self.cursor, self.company, data["assignee_uuid"], lock=True):
            raise DeskError(404, "STAFF_NOT_FOUND", "직원을 찾을 수 없습니다.")
        ident = repository.add_contact(self.cursor, self.company, self.staff, data, reminders=reminders)
        return self.contact(ident)

    def contacts(self, filters=None):
        return repository.contacts(self.cursor, self.company, self.staff, self.can_read, self.can_chat, filters=filters)

    def contact(self, ident, *, lock=False):
        rows = repository.contacts(self.cursor, self.company, self.staff, self.can_read, self.can_chat,
                                   ident=ident, lock=lock)
        if not rows:
            raise DeskError(404, "CONTACT_NOT_FOUND", "연락 기록을 찾을 수 없습니다.")
        return rows[0]

    def complete(self, ident, data):
        record = self.contact(ident, lock=True)
        contacted_at, result = data["contacted_at"], data["result"]
        if record["contacted_at"] is not None:
            if (record["completed_by_uuid"], record["contacted_at"], record["result"]) == (
                self.staff, contacted_at, result
            ):
                return record
            raise DeskError(409, "CONTACT_COMPLETED", "이미 완료된 연락입니다.")
        repository.complete_contact(self.cursor, ident, self.staff, contacted_at, result)
        return self.contact(ident)

    def require_chat(self):
        if not self.can_chat:
            raise DeskError(403, "WORK_PERMISSION_REQUIRED", "채팅 상담 권한이 없습니다.")

    def chat(self, ident):
        self.require_chat()
        record = repository.chat_detail(self.cursor, self.company, ident)
        if record is None:
            raise DeskError(404, "CHAT_NOT_FOUND", "상담을 찾을 수 없습니다.")
        return record


@contextmanager
def open_desk(token):
    with transaction.atomic(), connection.cursor() as cursor:
        actor = current_actor(token, cursor)
        desk = FollowUpDesk(cursor, actor)
        if not (desk.can_read or desk.can_chat):
            raise DeskError(403, "WORK_PERMISSION_REQUIRED", "팔로업을 조회할 권한이 없습니다.")
        yield desk
