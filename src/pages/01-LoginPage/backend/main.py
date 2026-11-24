import logging
import os
import re
from contextlib import closing
from secrets import token_urlsafe

import psycopg2
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr

from .tickets import SESSION_COOKIE, SESSION_SECONDS, issue_session, register_ticket_routes, secure_cookie


DOMAIN_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
EMAIL_PATTERN = re.compile(
    rf"[a-z0-9]{{5,15}}@{DOMAIN_LABEL}(?:\.{DOMAIN_LABEL})+",
    re.ASCII | re.IGNORECASE,
)
logger = logging.getLogger(__name__)


def find_password_hash(dsn: str, email: str) -> str | None:
    with closing(psycopg2.connect(dsn, connect_timeout=5)) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT staff.password_hash, company.email_domain
                FROM staff_user AS staff
                JOIN company ON company.company_uuid = staff.company_uuid
                WHERE lower(staff.email) = %s
                LIMIT 2
                """,
                (email,),
            )
            rows = cursor.fetchall()

    # An email-only login must identify exactly one employee.
    if len(rows) != 1:
        return None
    password_hash, company_domain = rows[0]
    if (
        not company_domain
        or company_domain.lower() != email.split("@", 1)[1]
        or not password_hash
        or not password_hash.startswith("$argon2id$")
    ):
        return None
    return password_hash


class LoginRequest(BaseModel):
    email: str = Field(max_length=254)
    password: SecretStr = Field(max_length=1024)


def create_app() -> FastAPI:
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("DATABASE_URL is required for login.")

    app = FastAPI()
    hasher = PasswordHasher()
    dummy_hash = hasher.hash(token_urlsafe(32))

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, _exc: RequestValidationError):
        message = ("이메일과 비밀번호를 문자열로 입력해 주세요." if request.url.path == "/api/auth/login"
                   else "조회 조건의 형식과 범위를 확인해 주세요.")
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "INVALID_REQUEST",
                    "message": message,
                },
            },
        )

    @app.middleware("http")
    async def prevent_shared_cache(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(psycopg2.Error)
    async def database_unavailable(_request: Request, error: psycopg2.Error):
        logger.error("Database request failed (%s).", type(error).__name__)
        return JSONResponse(status_code=503, content={"detail": {
            "code": "SERVICE_UNAVAILABLE", "message": "데이터를 조회할 수 없습니다. 잠시 후 다시 시도해 주세요.",
        }})

    @app.post("/api/auth/login")
    def login(credentials: LoginRequest, request: Request, response: Response) -> dict[str, str]:
        email = credentials.email
        if not EMAIL_PATTERN.fullmatch(email):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "INVALID_EMAIL",
                    "message": (
                        "회사 이메일을 입력해 주세요."
                        if not email
                        else "회사 이메일 형식을 확인해 주세요."
                    ),
                },
            )

        password = credentials.password.get_secret_value()
        if not password:
            raise HTTPException(
                status_code=422,
                detail={"code": "PASSWORD_REQUIRED", "message": "비밀번호를 입력해 주세요."},
            )

        email = email.lower()
        try:
            password_hash = find_password_hash(dsn, email)
        except psycopg2.Error as error:
            logger.error("Login account lookup failed (%s).", type(error).__name__)
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "AUTH_UNAVAILABLE",
                    "message": "로그인 요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.",
                },
            ) from None

        # Verify a dummy hash for unknown emails to avoid an immediate failure.
        try:
            matches = hasher.verify(password_hash or dummy_hash, password)
        except (InvalidHashError, VerificationError):
            matches = False

        if password_hash is None or not matches:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "INVALID_CREDENTIALS",
                    "message": "이메일 또는 비밀번호를 확인해 주세요.",
                },
            )

        token = issue_session(dsn, email, password_hash)
        response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_SECONDS, path="/api",
                            secure=secure_cookie(request), httponly=True, samesite="lax")
        return {"email": email}

    register_ticket_routes(app, dsn)
    return app
