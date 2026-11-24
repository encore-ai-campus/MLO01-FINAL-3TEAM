import logging
from secrets import token_urlsafe

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from django.db import DatabaseError, connection

from config.http import DeskError
from . import repository

logger = logging.getLogger(__name__)
hasher = PasswordHasher()
dummy_hash = hasher.hash(token_urlsafe(32))


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
            return actor
    raise DeskError(401, "LOGIN_REQUIRED", "로그인해 주세요.")


def logout(token):
    if token and len(token) <= 128:
        repository.revoke(token)
