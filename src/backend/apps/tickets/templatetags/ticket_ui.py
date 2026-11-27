"""Display rules and GET form state for the Korean ticket desk."""
from datetime import datetime
from zoneinfo import ZoneInfo

from django import template
from django.utils import timezone

register = template.Library()
KST = ZoneInfo("Asia/Seoul")
SORT_LABELS = {
    "default": "접수 오래된순",
    "updated-desc": "업데이트 최신순",
    "updated-asc": "업데이트 오래된순",
    "received-desc": "접수일자 최신순",
    "received-asc": "접수일자 오래된순",
}
BADGES = {
    "미처리": "amber", "보류": "blue", "답변완료": "green",
    "긍정": "green", "중립": "amber", "부정": "red", "매우 부정": "strong-red",
    "미배정": "neutral", "분류 대기": "neutral", "분석 중": "neutral", "분석 실패": "red",
    "분류 보류": "amber", "담당 부서 미등록": "amber",
}


@register.filter
def analysis_label(ticket, field):
    state = ticket.get(field + "_status")
    if state == "succeeded":
        if field == "category":
            reason = ticket.get("category_review_reason")
            if reason == "classification_abstained":
                return "분류 보류"
            if reason == "no_team_mapping":
                return "담당 부서 미등록"
        return ticket.get(field) or ("미분류" if field == "category" else "결과 없음")
    return {"running": "분석 중", "failed": "분석 실패"}.get(state, "분류 대기")


@register.filter
def badge_color(label):
    return BADGES.get(label, "")


@register.filter
def channel_label(value):
    return {"discord": "Discord", "gmail": "Gmail"}.get(value, value)


@register.filter
def message_label(message):
    labels = {"inquiry": "고객 문의", "reply": "상담사 답변", "memo": "내부 메모"}
    return labels.get(message["message_type"], "고객" if message["sender_type"] == "customer" else "상담사")


@register.filter
def delivery_label(value):
    return {"sending": "전송 중", "sent": "전송 완료", "failed": "전송 실패",
            "unknown": "전송 결과 확인 필요", "internal_only": "내부 기록"}.get(value, value)


def local_time(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.astimezone(KST)


@register.filter
def detail_time(value):
    return local_time(value).strftime("%Y/%m/%d %H:%M")


@register.filter
def received_label(value):
    date = local_time(value)
    today = timezone.now().astimezone(KST).date()
    if date.date() == today:
        return date.strftime("%H:%M")
    return date.strftime("%m/%d" if date.year == today.year else "%Y/%m/%d")


@register.filter
def updated_label(value):
    date = local_time(value)
    now = timezone.now().astimezone(KST)
    minutes = int((now - date).total_seconds() // 60)
    if minutes < 1:
        return "1분 미만"
    if minutes < 60:
        return f"{minutes}분 전"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}시간 전"
    if hours // 24 <= 7:
        return f"{hours // 24}일 전"
    if now.year != date.year:
        return "지난해"
    return "지난달" if now.month != date.month else "지난주"


@register.filter
def timestamp(value):
    return local_time(value).isoformat()


@register.simple_tag
def retained_fields(query, excluded):
    """A GET form must submit every retained value, including repeated filters."""
    excluded = set(excluded.split())
    return [(name, value) for name, values in query.lists() if name not in excluded for value in values]


@register.simple_tag
def list_controls(query, options, page, page_size, total):
    def option_rows(key):
        return [(str(row["id"]), row["name"]) for row in options[key]]

    definitions = [
        ("team", "담당 부서", [("unassigned", "미배정"), *option_rows("teams")]),
        ("assignee", "담당자", [("unassigned", "미지정"), *option_rows("assignees")]),
        ("status", "처리 상태", [(label, label) for label in ("미처리", "보류", "답변완료")]),
        ("category", "카테고리", option_rows("categories")),
        ("sentiment", "감성", [(label, label) for label in ("긍정", "중립", "부정", "매우 부정")]),
    ]
    labels, groups = [], []
    for name, caption, choices in definitions:
        selected = set(query.getlist(name))
        groups.append((name, caption, [(key, text, key in selected) for key, text in choices]))
        labels.extend(text for key, text in choices if key in selected)
    if query.get("start") and query.get("end"):
        labels.append(f"{query['start']} ~ {query['end']}")
    elif query.get("days"):
        labels.append(f"최근 {query['days']}일")

    pages = (total + page_size - 1) // page_size
    first = 1 + (page - 1) // 10 * 10
    last = min(first + 9, pages)
    sort = query.get("sort", "default")
    orders = list(SORT_LABELS)
    return {
        "groups": groups, "labels": labels,
        "page_sizes": (10, 20, 30, 50), "periods": ("1", "3", "7", "30"),
        "pages": pages, "numbers": range(first, last + 1),
        "previous_group": first - 10 if first > 1 else None,
        "next_group": last + 1 if last < pages else None,
        "offset": (page - 1) * page_size,
        "sort_label": SORT_LABELS[sort], "descending": sort.endswith("desc"),
        "next_sort": orders[(orders.index(sort) + 1) % len(orders)],
        "today": timezone.now().astimezone(KST).date().isoformat(),
    }


@register.filter
def message_time(value):
    return local_time(value).strftime("%H:%M")
