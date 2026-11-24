from uuid import UUID

from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.presentation.views import session_token
from config.http import DeskError, DeskJSONEncoder, validate_form
from .. import reply, services
from .forms import MemoForm, parse_query


@require_GET
def ticket_list(request, as_json=False):
    with services.open_desk(session_token(request)) as desk:
        params = request.GET.copy()
        if not as_json:
            for key in ("days", "start", "end"):
                if key in params and not params[key]:
                    del params[key]
        query = parse_query(params)
        result = desk.list_tickets(query)
        if as_json:
            return JsonResponse(result, encoder=DeskJSONEncoder)
        options = desk.filter_options()
        display = services.display_token(desk, [row["ticket_uuid"] for row in result["items"]])
    return render(request, "tickets/list.html", {
        **result, "actor": desk.actor, "options": options, "scope": query.scope,
        "active": "mine" if query.scope == "mine" else "tickets", "prediction_display": display,
    })


@require_GET
def options(request):
    with services.open_desk(session_token(request)) as desk:
        return JsonResponse(desk.filter_options())


@require_GET
def detail(request, ticket_uuid, as_json=False):
    with services.open_desk(session_token(request)) as desk:
        try:
            ticket_uuid = UUID(str(ticket_uuid))
        except ValueError:
            raise DeskError(422, "INVALID_REQUEST", "조회 조건의 형식과 범위를 확인해 주세요.") from None
        result = desk.detail(ticket_uuid)
        if as_json:
            return JsonResponse(result, encoder=DeskJSONEncoder)
        display = services.display_token(desk, [ticket_uuid])
        reply = services.reply_options(desk, result["ticket"])
    return render(request, "tickets/detail.html", {
        **result, "actor": desk.actor, "scope": request.GET.get("scope", "all"), "active": "detail",
        "prediction_display": display, "reply": reply,
    })


@require_POST
def add_memo(request, ticket_uuid):
    data = validate_form(MemoForm, request)
    memo = services.add_memo(session_token(request), ticket_uuid, data["body"])
    return JsonResponse(memo, status=201, encoder=DeskJSONEncoder)


@require_POST
def prediction_display(request):
    values = request.POST.getlist("displayed")
    if values and len(values) <= 100:
        services.record_display(session_token(request), request.POST.get("token", ""), set(values))
    return HttpResponse(status=204)


@require_POST
def update_ticket(request, ticket_uuid, complete=False):
    services.update_ticket(session_token(request), ticket_uuid, request.POST, complete=complete)
    return redirect("tickets:detail", ticket_uuid=ticket_uuid)


@require_POST
def send_reply(request, ticket_uuid):
    reply.send(session_token(request), ticket_uuid, request.POST)
    return redirect("tickets:detail", ticket_uuid=ticket_uuid)


@require_GET
def monitoring_readiness(request):
    return JsonResponse({"reply_connected": bool(reply.reply_endpoint())})
