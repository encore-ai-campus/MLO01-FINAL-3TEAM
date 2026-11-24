from django import forms
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.presentation.views import session_token
from config.http import DeskError, DeskJSONEncoder
from .. import services


class ReviewReplyForm(forms.Form):
    body = forms.CharField(max_length=10_000)


@require_GET
def inbox(request, request_id=None):
    with services.open_inbox(session_token(request)) as (cursor, actor):
        context = services.inbox_context(cursor, actor, request.GET, request_id)
    return render(request, "collaboration/inbox.html", {"actor": actor, "active": "inbox", **context})


@require_POST
def read(request, kind, item_id):
    with services.open_inbox(session_token(request), write=True) as (cursor, actor):
        read_at = services.mark_read(cursor, actor, kind, item_id)
    return JsonResponse({"read_at": read_at}, encoder=DeskJSONEncoder)


@require_POST
def reply(request, request_id):
    with services.open_inbox(session_token(request), write=True) as (cursor, actor):
        form = ReviewReplyForm(request.POST)
        if not form.is_valid():
            raise DeskError(422, "INVALID_REPLY", "답변 내용을 확인해 주세요.")
        services.reply(cursor, actor, request_id, form.cleaned_data["body"])
    return JsonResponse({"ok": True})
