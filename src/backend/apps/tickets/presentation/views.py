from uuid import UUID

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.accounts.presentation.views import session_token
from config.http import DeskError, DeskJSONEncoder
from .. import services
from .forms import parse_query


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
    return render(request, "tickets/list.html", {
        **result, "actor": desk.actor, "options": options, "scope": query.scope,
        "active": "mine" if query.scope == "mine" else "tickets",
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
    return render(request, "tickets/detail.html", {
        **result, "actor": desk.actor, "scope": request.GET.get("scope", "all"), "active": "detail",
    })
