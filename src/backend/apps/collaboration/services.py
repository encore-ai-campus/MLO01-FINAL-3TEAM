from contextlib import contextmanager, nullcontext
from uuid import UUID

from django.db import connection, transaction

from apps.accounts.services import current_actor
from config.database import read_snapshot
from config.http import DeskError
from . import repository

TABS = {
    "transfer": "받은 이관 요청", "review": "받은 검토 요청",
    "sent_transfer": "보낸 이관 요청", "sent_review": "보낸 검토 요청",
}
SENTIMENTS = {"긍정": "positive", "중립": "neutral", "부정": "negative", "매우 부정": "very-negative",
              "분석 실패": "negative"}
REPLY_ALLOWED = {"pending", "accepted"}
TRANSFER_STATUS = {"pending": "수락 대기", "accepted": "수락 완료", "rejected": "거절"}
REVIEW_STATUS = {"pending": "검토 대기", "accepted": "검토 중", "rejected": "거절", "replied": "회신 완료"}


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
    raise DeskError(404, "INBOX_ITEM_NOT_FOUND", "요청 항목을 찾을 수 없습니다.")


def request_rows(cursor, actor):
    scope = _scope(actor)
    for is_sent, requests in ((False, repository.received(cursor, scope)), (True, repository.sent(cursor, scope))):
        for item in requests:
            kind = item["kind"]
            status = item["request_status"]
            item["is_sent"] = is_sent
            item["tab"] = f"sent_{kind}" if is_sent else kind
            item["counterpart"] = item["counterpart"] if is_sent else item["sender"]
            item["tracks_read"] = not is_sent or bool(item.get("reply_id"))
            item["unread"] = item["tracks_read"] and item["read_at"] is None
            item["can_reply"] = not is_sent and kind == "review" and not item["sent_reply"] and status in REPLY_ALLOWED
            item["needs_action"] = item["can_reply"] or (not is_sent and kind == "transfer" and status == "pending")
            statuses = TRANSFER_STATUS if kind == "transfer" else REVIEW_STATUS
            item["status_label"] = statuses.get(status, "—")
            yield item


def inbox_context(cursor, actor, params, request_id=None):
    items = list(request_rows(cursor, actor))
    selected = None
    item_id = request_id or params.get("request")
    if item_id:
        try:
            item_id = UUID(str(item_id))
        except ValueError:
            missing()
        selected = next((item for item in items if item_id in (item["id"], item.get("reply_id"))), None)
        if selected is None:
            missing()
    requested_tab = params.get("tab")
    tab = selected["tab"] if selected else requested_tab
    if tab == "reply":
        tab = "sent_review"
    if tab not in TABS:
        tab = "transfer"
    is_sent = tab.startswith("sent_")
    has_unread_filter = tab != "sent_transfer"
    search = params.get("q", "").strip()
    key = search.casefold()
    scope = "unread" if has_unread_filter and requested_tab != "reply" and params.get("scope") == "unread" else "all"
    counts = dict.fromkeys(TABS, 0)
    attention = dict.fromkeys(TABS, 0)
    rows = []
    for item in items:
        counts[item["tab"]] += 1
        attention[item["tab"]] += item["unread"] or item["needs_action"]
        if item["tab"] != tab or (scope == "unread" and not item["unread"]):
            continue
        if any(key in value.casefold() for value in (item["title"], "#" + item["ticket_number"], item["counterpart"] or "")):
            rows.append(item)
    history = repository.transfer_history(cursor, actor["company_uuid"], selected["ticket_uuid"]) if selected and selected["kind"] == "transfer" else []
    return {"tabs": [(kind, label, counts[kind], attention[kind]) for kind, label in TABS.items()], "tab": tab,
            "tab_label": TABS[tab], "column": "이관 메시지" if tab.endswith("transfer") else "검토 메모",
            "person_label": "받는 사람" if is_sent else "보낸 사람", "time_label": "보낸 시각" if is_sent else "받은 시각",
            "has_unread_filter": has_unread_filter,
            "scope": scope, "query": search, "selected": selected, "rows": rows, "transfer_history": history,
            "sentiment_class": SENTIMENTS.get(selected["sentiment"], "pending") if selected else ""}


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
