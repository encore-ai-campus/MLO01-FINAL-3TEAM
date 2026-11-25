from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from apps.accounts.presentation.views import session_token
from config.http import DeskJSONEncoder, validate_form
from ..services import open_desk
from .forms import CompleteForm, ContactForm, ContactQueryForm


def response(data, status=200):
    return JsonResponse(data, status=status, encoder=DeskJSONEncoder)


def with_source(row):
    ticket = row["ticket_uuid"]
    if ticket:
        return {**row, "source": "ticket", "source_url": reverse("tickets:detail", args=[ticket])}
    return {**row, "source": "chat", "source_url": reverse("followup:chat", args=[row["chat_uuid"]])}


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
