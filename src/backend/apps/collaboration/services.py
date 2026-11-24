from .repository import preview_data


INBOX_TABS = {"transfer": "받은 이관", "review": "받은 검토 요청", "reply": "받은 검토 답변"}
SENTIMENTS = {"긍정": "positive", "중립": "neutral", "부정": "negative", "매우 부정": "very-negative", "분석 중": "pending"}


def inbox_context(params, request_id=None):
    data = preview_data()
    groups = data["requestGroups"]
    tab = params.get("tab", "transfer")
    tab = tab if tab in INBOX_TABS else "transfer"
    empty = params.get("empty") == "1"
    search = params.get("q", "").strip()
    rows = [] if empty else groups.get(tab, [])
    selected_id = request_id or params.get("request", "")
    selected = next((r for rs in groups.values() for r in rs if r["requestId"] == selected_id), None)
    if empty:
        selected = None
    return {
        "tab": tab, "tabs": INBOX_TABS.items(), "empty_demo": empty, "query": search,
        "scope": "unread" if params.get("scope") == "unread" else "all",
        "column": {"transfer": "이관 메시지", "review": "검토 메모", "reply": "답변"}[tab],
        "rows": [r for r in rows if any(search.casefold() in s.casefold() for s in (r["title"], "#" + r["id"], r["sender"]))],
        "selected": selected, "profile": data["ticketProfiles"].get(selected["id"]) if selected else None,
        "is_review": bool(selected and selected["requestId"].startswith("review-")),
        "sentiment_class": SENTIMENTS.get(selected["sentiment"], "pending") if selected else "",
        "reply_requests": groups["review"] if not empty else [],
        "unread_ids": {key: [r["requestId"] for r in value if r["unread"]] if not empty else [] for key, value in groups.items()},
    }
