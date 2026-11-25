from collections import defaultdict
from datetime import timedelta
from urllib.parse import urlencode

from django import forms
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.accounts.presentation.views import session_token
from config.http import DeskError

from ..services import open_desk
from .forms import CompleteForm, TrackScheduleForm


TABS = {"tracked": "추적 티켓", "scheduled": "연락 예정", "completed": "연락 완료"}
REMINDERS = ((60, "1시간 전"), (30, "30분 전"), (10, "10분 전"), (5, "5분 전"), (0, "정시"))
NOTICES = {"registered": "추적을 등록했습니다.", "removed": "추적을 해제했습니다.",
           "completed": "연락 완료를 저장했습니다."}


class CompleteContactForm(CompleteForm):
    contact_uuid = forms.UUIDField()


class RemoveTrackForm(forms.Form):
    ticket_uuid = forms.UUIDField()


def page_url(tab, *, selected="", query="", notice=""):
    params = {"tab": tab, "selected": selected, "q": query, "notice": notice}
    return reverse("followup:page") + "?" + urlencode({key: value for key, value in params.items() if value})


def save(desk, action, data):
    if action == "register":
        desk.track_and_schedule(data)
        return page_url("tracked", selected=data["ticket_uuid"], notice="registered")
    if action == "untrack":
        desk.untrack(data["ticket_uuid"])
        return page_url("tracked", notice="removed")
    desk.complete(data["contact_uuid"], data)
    return page_url("completed", selected=data["contact_uuid"], notice="completed")


def contact_row(row):
    ticket = row["ticket_uuid"]
    return {**row, "id": row["contact_uuid"],
            "number": f'#{row["ticket_number"]}' if ticket else f'채팅 #{str(row["chat_uuid"])[:8]}',
            "title": row["title"] or "1:1 상담",
            "source_url": (reverse("tickets:detail", args=[ticket]) if ticket
                           else reverse("followup:chat-page", args=[row["chat_uuid"]]))}


def activity(tracks, contacts, events):
    history = list(events)
    tracked = {row["ticket_uuid"] for row in tracks}
    for row in contacts:
        started, stopped = row["reminders_started_at"], row["reminders_stopped_at"]
        if started is None:
            continue
        for minutes, label in REMINDERS:
            moment = row["scheduled_at"] - timedelta(minutes=minutes)
            if moment >= started and (stopped is None or moment <= stopped):
                history.append({**row, "occurred_at": moment, "label": f"후속 연락 {label} 알림",
                                "unread": row["ticket_uuid"] in tracked and stopped is None})
    return sorted(history, key=lambda item: item["occurred_at"], reverse=True)


def page_context(desk, request):
    tabs = TABS if desk.can_read else {key: TABS[key] for key in ("scheduled", "completed")}
    selected_tab = request.GET.get("tab")
    tab = selected_tab if selected_tab in tabs else next(iter(tabs))
    query = request.GET.get("q", "").strip()[:100]
    selected = request.GET.get("selected", "")
    now = timezone.now()
    contacts = [contact_row(row) for row in desk.contacts()]
    events = []
    tracks = []
    if desk.can_read:
        events_by_ticket = defaultdict(list)
        for event in desk.notifications():
            event = {**event, "number": f'#{event["ticket_number"]}', "unread": True,
                     "label": (f'{event["author"]} 상담사 메모' if event["kind"] == "memo" else "고객 후속 메시지")}
            events.append(event)
            events_by_ticket[event["ticket_uuid"]].append(event)
        for track in desk.tracks():
            ticket = track["ticket_uuid"]
            tracks.append({**track, "id": ticket, "number": f'#{track["ticket_number"]}',
                           "source_url": reverse("tickets:detail", args=[ticket]), "events": events_by_ticket[ticket]})

    if tab == "tracked":
        rows = sorted(tracks, key=lambda row: row["events"][0]["occurred_at"] if row["events"] else row["tracked_at"],
                      reverse=True)
    elif tab == "completed":
        rows = sorted((row for row in contacts if row["contacted_at"]), key=lambda row: row["contacted_at"], reverse=True)
    else:
        rows = sorted((row for row in contacts if not row["contacted_at"]), key=lambda row: row["scheduled_at"])

    term = query.casefold()
    rows = [row for row in rows if term in row["number"].casefold() or term in row["title"].casefold()]
    for row in rows:
        row["url"] = page_url(tab, selected=row["id"], query=query)
    detail = next((row for row in rows if str(row["id"]) == selected), rows[0] if rows else None)
    choices = desk.track_options() if tab == "tracked" else []
    return {"actor": desk.actor, "active": "followup", "tab": tab, "tab_name": tabs[tab], "query": query,
            "tabs": [(key, label, page_url(key)) for key, label in tabs.items()],
            "rows": rows, "detail": detail, "now": now, "track_options": choices,
            "show_followup_history": desk.can_read, "followup_history": activity(tracks, contacts, events),
            "notice": NOTICES.get(request.GET.get("notice")),
            "reminder_summary": " · ".join(label for _, label in REMINDERS)}


@require_http_methods(["GET", "POST"])
def index(request):
    token = session_token(request)
    action = request.POST.get("action", "")
    error = None
    status = 200
    if request.method == "POST":
        form_type = {"register": TrackScheduleForm, "untrack": RemoveTrackForm, "complete": CompleteContactForm}.get(action)
        if form_type is None:
            raise DeskError(400, "INVALID_ACTION", "잘못된 팔로업 요청입니다.")
        form = form_type(request.POST)
        try:
            with open_desk(token) as desk:
                if form.is_valid():
                    return redirect(save(desk, action, form.cleaned_data))
                error = next(iter(form.errors.values()))[0]
                status = 422
        except DeskError as exc:
            if exc.status not in (404, 409):
                raise
            error, status = exc.message, exc.status

    with open_desk(token) as desk:
        context = page_context(desk, request)
    return render(request, "followup/page.html", {**context, "error": error, "action": action,
                                                 "submitted": request.POST}, status=status)
