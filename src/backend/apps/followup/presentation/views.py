from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from apps.accounts.presentation.views import session_token
from config.http import DeskJSONEncoder, record_output, validate_form
from ..services import open_desk
from .forms import CompleteForm, ContactForm, ContactQueryForm, TrackScheduleForm


def response(data, status=200):
    return JsonResponse(record_output(data), status=status, encoder=DeskJSONEncoder)


def with_source(row):
    ticket = row["ticket_id"]
    if ticket:
        return {**row, "source": "ticket", "source_url": reverse("tickets:detail", args=[ticket])}
    return {**row, "source": "chat", "source_url": reverse("followup:chat", args=[row["chat_id"]])}


@require_GET
def page_data(request):
    with open_desk(session_token(request)) as desk:
        return response({
            "tickets": desk.track_options() if desk.can_read else [],
            "tracks": [{
                **with_source(row), "remove_url": reverse("followup:track", args=[row["ticket_id"]]),
            } for row in desk.tracks()] if desk.can_read else [],
            "contacts": [{
                **with_source(row), "complete_url": reverse("followup:complete", args=[row["contact_id"]]),
            } for row in desk.contacts()],
            "notifications": desk.notifications() if desk.can_read else [],
        })


@require_POST
def track_schedule(request):
    with open_desk(session_token(request)) as desk:
        contact = desk.track_and_schedule(validate_form(TrackScheduleForm, request))
        return response(with_source(contact), 201)


@require_GET
def chat_page(request, chat_id):
    with open_desk(session_token(request)) as desk:
        context = {"actor": desk.actor, "active": "followup", "chat": desk.chat(chat_id)}
    return render(request, "followup/chat.html", context)


@require_GET
def tracks(request):
    with open_desk(session_token(request)) as desk:
        return response({"items": [with_source(row) for row in desk.tracks()]})


@require_http_methods(["POST", "DELETE"])
def track(request, ticket_id):
    with open_desk(session_token(request)) as desk:
        if request.method == "DELETE":
            desk.untrack(ticket_id)
            return HttpResponse(status=204)
        created = desk.track(ticket_id)
        return response({"ticket_id": ticket_id}, 201 if created else 200)


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
def contact(request, contact_id):
    with open_desk(session_token(request)) as desk:
        return response(with_source(desk.contact(contact_id)))


@require_POST
def complete(request, contact_id):
    with open_desk(session_token(request)) as desk:
        return response(with_source(desk.complete(contact_id, validate_form(CompleteForm, request))))


@require_GET
def chat(request, chat_id):
    with open_desk(session_token(request)) as desk:
        return response(desk.chat(chat_id))
