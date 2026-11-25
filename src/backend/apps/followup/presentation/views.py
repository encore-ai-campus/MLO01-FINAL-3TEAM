from urllib.parse import urlencode

from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from apps.accounts.presentation.views import session_token
from config.http import DeskError, DeskJSONEncoder, validate_form
from ..services import open_desk
from .forms import CompleteForm, ContactForm, ContactQueryForm, RegistrationQueryForm


def response(data, status=200):
    return JsonResponse(data, status=status, encoder=DeskJSONEncoder)


def with_source(row):
    ticket = row["ticket_uuid"]
    if ticket:
        return {**row, "source": "ticket", "source_url": reverse("tickets:detail", args=[ticket])}
    return {**row, "source": "chat", "source_url": reverse("followup:chat", args=[row["chat_uuid"]])}


@require_GET
def page(request):
    with open_desk(session_token(request)) as desk:
        filters = validate_form(ContactQueryForm, request)
        tab = request.GET.get("tab") or ("tracks" if desk.can_read else "scheduled")
        if tab not in ("tracks", "scheduled", "completed"):
            raise DeskError(422, "INVALID_REQUEST", "팔로업 탭을 확인해 주세요.")
        now = timezone.now()
        if tab == "tracks":
            rows = desk.tracks()
            term = filters["q"].casefold()
            rows = [row for row in rows if term in f'#{row["ticket_number"]} {row["title"]}'.casefold()]
            id_key = "ticket_uuid"
        else:
            rows = desk.contacts({**filters, "state": tab})
            if tab == "scheduled":
                rows.sort(key=lambda row: (row["scheduled_at"], row["contact_uuid"]))
            id_key = "contact_uuid"
        items = [{**with_source(row), "selected_id": str(row[id_key])} for row in rows]
        selected = next((row for row in items if row["selected_id"] == request.GET.get("item")), None)
        events = []
        if tab == "tracks" and selected:
            events = [row for row in desk.notifications() if row["ticket_uuid"] == selected["ticket_uuid"]]
        context = {
            "actor": desk.actor, "active": "followup", "can_chat": desk.can_chat, "tab": tab,
            "items": items, "selected": selected, "events": events, "filters": filters, "now": now,
            "local_now": timezone.localtime(now).strftime("%Y-%m-%dT%H:%M"),
        }
    return render(request, "followup/page.html", context)


@require_http_methods(["GET", "POST"])
def register(request):
    with open_desk(session_token(request)) as desk:
        query = validate_form(RegistrationQueryForm, request)
        tracking = query["mode"] == "track"
        kind = query["source"] or ("ticket" if desk.can_read else "chat")
        if tracking and kind != "ticket":
            raise DeskError(422, "INVALID_REQUEST", "추적할 티켓을 선택해 주세요.")
        if kind == "ticket":
            desk.require_ticket_read()
        else:
            desk.require_chat()
        if request.method == "POST":
            fields = request.POST.copy()
            fields.pop("ticket_uuid", None)
            fields.pop("chat_uuid", None)
            fields[f"{kind}_uuid"] = fields.get("source_uuid", "")
            form = ContactForm(fields)
            if form.is_valid():
                contact = desk.track_and_schedule(form.cleaned_data) if tracking else desk.schedule(form.cleaned_data)
                item = contact["ticket_uuid"] if tracking else contact["contact_uuid"]
                return redirect(reverse("followup:page") + "?" + urlencode({
                    "tab": "tracks" if tracking else "scheduled", "item": item,
                }))
        else:
            form = ContactForm()
        sources = desk.registration_sources(kind, query["q"], tracking=tracking)
        return render(request, "followup/register.html", {
            "actor": desk.actor, "active": "followup", "can_chat": desk.can_chat, "kind": kind,
            "tracking": tracking, "mode": query["mode"] or "schedule", "q": query["q"],
            "sources": sources, "staff": desk.assignees(), "form": form,
        }, status=422 if form.errors else 200)


@require_POST
def remove(request, ticket_uuid):
    with open_desk(session_token(request)) as desk:
        desk.untrack(ticket_uuid)
    return redirect("followup:page")


@require_POST
def finish(request, contact_uuid):
    with open_desk(session_token(request)) as desk:
        desk.complete(contact_uuid, validate_form(CompleteForm, request))
    return redirect(reverse("followup:page") + "?" + urlencode({"tab": "completed", "item": contact_uuid}))


@require_GET
def chat_page(request, chat_uuid):
    with open_desk(session_token(request)) as desk:
        context = {"actor": desk.actor, "active": "followup", "chat": desk.chat(chat_uuid)}
    return render(request, "followup/chat.html", context)


@require_GET
def tracks(request):
    with open_desk(session_token(request)) as desk:
        return response({"items": [with_source(row) for row in desk.tracks()]})


@require_http_methods(["POST", "DELETE"])
def track(request, ticket_uuid):
    with open_desk(session_token(request)) as desk:
        if request.method == "DELETE":
            desk.untrack(ticket_uuid)
            return HttpResponse(status=204)
        created = desk.track(ticket_uuid)
        return response({"ticket_uuid": ticket_uuid}, 201 if created else 200)


@require_GET
def notifications(request):
    with open_desk(session_token(request)) as desk:
        if request.GET.get("summary") == "1":
            return response(desk.notifications(summary=True))
        items = desk.notifications()
        return response({"items": [with_source(row) for row in items], "count": len(items)})


@require_http_methods(["GET", "POST"])
def contacts(request):
    with open_desk(session_token(request)) as desk:
        if request.method == "POST":
            return response(with_source(desk.schedule(validate_form(ContactForm, request))), 201)
        filters = validate_form(ContactQueryForm, request)
        return response({"items": [with_source(row) for row in desk.contacts(filters)]})


@require_GET
def contact(request, contact_uuid):
    with open_desk(session_token(request)) as desk:
        return response(with_source(desk.contact(contact_uuid)))


@require_POST
def complete(request, contact_uuid):
    with open_desk(session_token(request)) as desk:
        return response(with_source(desk.complete(contact_uuid, validate_form(CompleteForm, request))))


@require_GET
def chat(request, chat_uuid):
    with open_desk(session_token(request)) as desk:
        return response(desk.chat(chat_uuid))
