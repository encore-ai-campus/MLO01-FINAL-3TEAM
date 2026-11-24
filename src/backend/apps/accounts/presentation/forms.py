import re

from config.http import DeskError

DOMAIN_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
EMAIL_PATTERN = re.compile(rf"[a-z0-9]{{5,15}}@{DOMAIN_LABEL}(?:\.{DOMAIN_LABEL})+", re.ASCII | re.IGNORECASE)


def credentials(data):
    email, password = data.get("email"), data.get("password")
    if not isinstance(email, str) or not isinstance(password, str) or len(email) > 254 or len(password) > 1024:
        raise DeskError(422, "INVALID_REQUEST", "이메일과 비밀번호를 문자열로 입력해 주세요.")
    if not EMAIL_PATTERN.fullmatch(email):
        raise DeskError(422, "INVALID_EMAIL", "회사 이메일 형식을 확인해 주세요." if email else "회사 이메일을 입력해 주세요.")
    if not password:
        raise DeskError(422, "PASSWORD_REQUIRED", "비밀번호를 입력해 주세요.")
    return email.lower(), password
