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
    staff = desk.actor["staff_user_uuid"]
    last_reply = next((row for row in reversed(result["messages"])
                       if row["message_type"] == "reply" and row["staff_user_uuid"] == staff), None)
    uncertain = any(row["delivery_status"] in ("sending", "unknown") for row in result["messages"])
    retry_draft = last_reply["body"] if last_reply and last_reply["delivery_status"] == "failed" and not uncertain else ""
    conversation = []
    previous_customer = None
    for entry in sorted(result["messages"] + result["memos"], key=lambda row: row["created_at"]):
        customer = entry["sender_type"] == "customer"
        author = entry["author_name"] if customer else None
        conversation.append({**entry, "show_avatar": not customer or author != previous_customer})
        previous_customer = author
    return render(request, "tickets/detail.html", {
        **result, "conversation": conversation, "actor": desk.actor, "scope": request.GET.get("scope", "all"), "active": "detail",
        "customer_tickets": sorted(result["customer_tickets"], key=lambda row: row["updated_at"], reverse=True),
        "is_assignee": result["ticket"]["assignee_uuid"] == desk.actor["staff_user_uuid"],
        "show_memos": request.GET.get("memos") == "1",
        "request_key": uuid4(), "retry_draft": retry_draft, "delivery_uncertain": uncertain,
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
        message = action_forms.message_payload(data)
        result = actions.store_message(token, ticket_uuid, *message)
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
    params = request.GET.copy()
    if action == "messages" and message[0] == "memo":
        params["memos"] = "1"
    return redirect(f"{url}?{params.urlencode()}" if params else url)


@require_POST
def complete_ticket(request, ticket_uuid):
    actions.complete_ticket(session_token(request), ticket_uuid)
    return redirect(reverse("tickets:detail", args=(ticket_uuid,), query=request.GET))


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
