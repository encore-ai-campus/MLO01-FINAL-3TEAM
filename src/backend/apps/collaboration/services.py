from contextlib import contextmanager, nullcontext
from uuid import UUID

from django.db import connection, transaction

from apps.accounts.services import current_actor
from config.database import read_snapshot
from config.http import DeskError
from . import repository

TABS = {"transfer": "받은 이관", "review": "받은 검토 요청", "reply": "받은 검토 답변"}
COLUMNS = {"transfer": "이관 메시지", "review": "검토 메모", "reply": "답변"}
SENTIMENTS = {"긍정": "positive", "중립": "neutral", "부정": "negative", "매우 부정": "very-negative",
              "분석 실패": "negative"}
REPLY_ALLOWED = {"pending", "accepted"}


@contextmanager
def open_inbox(token, *, write=False):
    if not token or len(token) > 128:
        raise DeskError(401, "LOGIN_REQUIRED", "로그인해 주세요.")
    with transaction.atomic() if write else read_snapshot() as snapshot:
        with connection.cursor() if write else nullcontext(snapshot) as cursor:
            actor = current_actor(token, cursor)
            if not actor["can_read"]:
                raise DeskError(403, "TICKET_READ_FORBIDDEN", "티켓 조회 권한이 없습니다.")
            yield cursor, actor


def _scope(actor):
    return actor["company_uuid"], actor["staff_user_uuid"]


def missing():
    raise DeskError(404, "INBOX_ITEM_NOT_FOUND", "받은 항목을 찾을 수 없습니다.")


def inbox_context(cursor, actor, params, request_id=None):
    items = repository.received(cursor, _scope(actor))
    selected = None
    item_id = request_id or params.get("request")
    if item_id:
        try:
            item_id = UUID(str(item_id))
        except ValueError:
            missing()
        selected = next((item for item in items if UUID(str(item["id"])) == item_id), None)
        if selected is None:
            missing()
    tab = params.get("tab")
    if tab not in TABS:
        tab = selected["kind"] if selected else "transfer"
    search = params.get("q", "").strip() if tab != "reply" else ""
    key = search.casefold()
    scope = "unread" if tab != "reply" and params.get("scope") == "unread" else "all"
    counts = dict.fromkeys(TABS, 0)
    rows = []
    for item in items:
        counts[item["kind"]] += item["read_at"] is None
        if item["kind"] != tab or (scope == "unread" and item["read_at"] is not None):
            continue
        if any(key in value.casefold() for value in (item["title"], "#" + item["ticket_number"], item["sender"] or "")):
            rows.append(item)
    history = repository.transfer_history(cursor, actor["company_uuid"], selected["ticket_uuid"]) if selected and selected["kind"] == "transfer" else []
    can_reply = (selected and selected["kind"] == "review" and not selected["sent_reply"]
                 and selected["request_status"] in REPLY_ALLOWED)
    return {"tabs": [(kind, label, counts[kind]) for kind, label in TABS.items()], "tab": tab, "column": COLUMNS[tab],
            "scope": scope, "query": search, "selected": selected, "rows": rows, "transfer_history": history,
            "can_reply": can_reply, "sentiment_class": SENTIMENTS.get(selected["sentiment"], "pending") if selected else ""}


def mark_read(cursor, actor, kind, item_id):
    write_read = {"request": repository.mark_request_read, "reply": repository.mark_reply_read}.get(kind)
    row = write_read(cursor, _scope(actor), item_id) if write_read else None
    if row is None:
        missing()
    return row[0]


def reply(cursor, actor, request_id, body):
    request = repository.lock_review(cursor, _scope(actor), request_id)
    if request is None:
        missing()
    status, existing = request
    if existing == body:
        return
    if existing or status not in REPLY_ALLOWED:
        raise DeskError(409, "REPLY_NOT_AVAILABLE", "이 요청에는 답변을 보낼 수 없습니다.")
    repository.insert_reply(cursor, request_id, actor["staff_user_uuid"], body)
