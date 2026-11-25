import logging
from secrets import token_urlsafe

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from django.db import DatabaseError, connection

from apps.preferences.navigation import visible_sidebar
from config.http import DeskError
from . import repository

logger = logging.getLogger(__name__)
hasher = PasswordHasher()
dummy_hash = hasher.hash(token_urlsafe(32))

TICKET_HANDLE = "ticket.handle"
CHAT_HANDLE = "chat.handle"
TEAM_REPLY_EDIT = "quick_reply.team.edit"
DASHBOARD_READ = "analytics.read"
WORK_PERMISSIONS = (
    (TICKET_HANDLE, "티켓 상담"),
    (CHAT_HANDLE, "채팅 상담"),
    (TEAM_REPLY_EDIT, "팀별 신속답변 편집"),
    (DASHBOARD_READ, "VoC 대시보드 접근"),
)
WORK_PERMISSION_CODES = tuple(code for code, _ in WORK_PERMISSIONS)


def login(email, password):
    try:
        password_hash = repository.find_password_hash(email)
        try:
            matches = hasher.verify(password_hash or dummy_hash, password)
        except (InvalidHashError, VerificationError):
            matches = False
        token = repository.issue_session(email, password_hash) if password_hash and matches else None
    except DatabaseError as error:
        logger.error("Login account lookup failed (%s).", type(error).__name__)
        raise DeskError(503, "AUTH_UNAVAILABLE", "로그인 요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.") from None
    if not token:
        raise DeskError(401, "INVALID_CREDENTIALS", "이메일 또는 비밀번호를 확인해 주세요.")
    return token


def current_actor(token, cursor=None):
    if token and len(token) <= 128:
        if cursor is None:
            with connection.cursor() as cursor:
                actor = repository.find_staff(cursor, token)
        else:
            actor = repository.find_staff(cursor, token)
        if actor:
            actor["visible_sidebar"] = visible_sidebar(actor)
            return actor
    raise DeskError(401, "LOGIN_REQUIRED", "로그인해 주세요.")


def require_settings_admin(actor):
    if actor.get("role") != "admin":
        raise DeskError(403, "SETTINGS_FORBIDDEN", "회사 관리자만 이 설정을 관리할 수 있습니다.")


def settings_employee(cursor, actor, staff_uuid, *, lock=False):
    staff = repository.company_employee(cursor, actor["company_uuid"], staff_uuid, lock=lock)
    if not staff:
        raise DeskError(404, "STAFF_NOT_FOUND", "직원을 찾을 수 없습니다.")
    return staff


def require_work_permission(cursor, actor, code):
    if code == DASHBOARD_READ:
        allowed = repository.dashboard_allowed(cursor, actor, code)
    else:
        allowed = repository.work_permissions(cursor, actor["company_uuid"], actor["staff_user_uuid"], (code,))
    if not allowed:
        raise DeskError(403, "WORK_PERMISSION_REQUIRED", "이 업무를 처리할 권한이 없습니다.")


def logout(token):
    if token and len(token) <= 128:
        repository.revoke(token)
