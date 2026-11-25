from uuid import UUID, uuid4

from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.presentation.views import session_token
from config.http import DeskError, DeskJSONEncoder, validate_form
from .. import actions, services
from . import action_forms
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
        result = desk.detail(ticket_uuid, with_controls=not as_json)
    if as_json:
        return JsonResponse(result, encoder=DeskJSONEncoder)
    return render(request, "tickets/detail.html", {
        **result, "actor": desk.actor, "scope": request.GET.get("scope", "all"), "active": "detail",
        "is_assignee": result["ticket"]["assignee_uuid"] == desk.actor["staff_user_uuid"],
        "request_key": uuid4(),
    })


@require_GET
def detail_controls(request, ticket_uuid, control):
    with services.open_desk(session_token(request)) as desk:
        result = desk.detail(ticket_uuid, with_controls=True)
    if not result["can_conduct"]:
        raise DeskError(403, "TICKET_CONDUCT_FORBIDDEN", "티켓 상담 권한이 없습니다.")
    if control == "quick-replies":
        return JsonResponse({"items": result["quick_replies"]}, encoder=DeskJSONEncoder)
    return JsonResponse({key: result[key] for key in ("transfer_recipients", "review_channels")},
                        encoder=DeskJSONEncoder)


@require_POST
def ticket_action(request, ticket_uuid, action, as_json=False):
    data = action_forms.fields(request)
    token = session_token(request)
    if action == "messages":
        result = actions.store_message(token, ticket_uuid, *action_forms.message_payload(data))
    elif action == "transfer":
        result = actions.transfer(token, ticket_uuid, action_forms.identifier(data.get("recipient_uuid")),
                                  action_forms.text(data.get("reason"), "이관 사유"),
                                  action_forms.text(data["attached_reply"], "첨부 답변") if data.get("attached_reply") else None)
    else:
        result = actions.request_review(token, ticket_uuid, action_forms.identifier(data.get("review_channel_uuid")),
                                        action_forms.text(data.get("memo"), "상세 메모"),
                                        action_forms.review_attachment(request, data))
    if as_json:
        return JsonResponse(result, encoder=DeskJSONEncoder, status=200 if action == "messages" else 201)
    url = reverse("tickets:detail", args=(ticket_uuid,))
    return redirect(f"{url}?{request.GET.urlencode()}" if request.GET else url)


@require_POST
def accept_transfer(request, ticket_uuid, request_uuid):
    actions.accept_transfer(session_token(request), ticket_uuid, request_uuid)
    return JsonResponse({"status": "accepted"})


@require_GET
def ticket_requests(request):
    kind = request.GET.get("kind")
    if kind not in (None, "transfer", "review"):
        raise action_forms.invalid("받은 요청 유형을 확인해 주세요.")
    return JsonResponse({"items": actions.received_requests(session_token(request), kind)}, encoder=DeskJSONEncoder)


@require_GET
def request_attachment(request, action_uuid):
    attachment = actions.request_attachment(session_token(request), action_uuid)
    response = HttpResponse(bytes(attachment["content"]), content_type=attachment["content_type"])
    response.headers["Content-Disposition"] = "attachment"
    return response

@require_POST
def add_memo(request, ticket_uuid):
    data = validate_form(MemoForm, request)
    memo = services.add_memo(session_token(request), ticket_uuid, data["body"])
    return JsonResponse(memo, status=201, encoder=DeskJSONEncoder)
