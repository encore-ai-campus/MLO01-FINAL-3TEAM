import json
import logging
from datetime import datetime
from urllib.parse import urlencode

from django.core.serializers.json import DjangoJSONEncoder
from django.db import DatabaseError
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.deprecation import MiddlewareMixin

logger = logging.getLogger(__name__)


class DeskJSONEncoder(DjangoJSONEncoder):
    def default(self, value):
        if isinstance(value, datetime):
            return value.isoformat()
        return super().default(value)


# Desk endpoints expose UUID fields independently of PostgreSQL column names.
_RECORD_UUID_FIELDS = (
    "actor", "assignee", "assigned_team", "attachment", "category", "channel", "chat",
    "chat_message", "collab_request", "collab_request_attachment", "company", "completed_by", "contact",
    "created_by", "domain", "recipient", "requester", "review_channel", "staff_user", "team",
    "ticket", "ticket_event", "ticket_message",
)
_RECORD_FIELD_NAMES = {f"{name}_id": f"{name}_uuid" for name in _RECORD_UUID_FIELDS}
_RECORD_FIELD_NAMES.update(feature_snapshot_id="feature_uuid", is_category_manual="category_manually_set")


def record_input(data, fields):
    for current in fields:
        previous = _RECORD_FIELD_NAMES.get(current)
        if previous and current not in data and previous in data:
            data = data.copy()
            data[current] = data[previous]
    return data


def record_output(value):
    if isinstance(value, dict):
        return {_RECORD_FIELD_NAMES.get(key, key): record_output(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [record_output(item) for item in value]
    return value


class DeskError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def validate_form(form_type, request):
    if request.method == "GET":
        data = request.GET
    elif request.content_type == "application/json":
        try:
            data = json.loads(request.body)
        except (ValueError, UnicodeDecodeError):
            data = None
        if not isinstance(data, dict):
            raise DeskError(422, "INVALID_REQUEST", "요청 본문을 확인해 주세요.")
    else:
        data = request.POST
    form = form_type(record_input(data, form_type.base_fields))
    if not form.is_valid():
        raise DeskError(422, "INVALID_REQUEST", "입력값의 형식과 범위를 확인해 주세요.")
    return form.cleaned_data


def error_response(request, error):
    if request.path.startswith("/api/"):
        return JsonResponse({"detail": {"code": error.code, "message": error.message}}, status=error.status)
    if error.code == "LOGIN_REQUIRED":
        return redirect(reverse("accounts:resume") + "?" + urlencode({"next": request.get_full_path()}))
    return render(request, "desk_error.html", {"error": error.message}, status=error.status)


def csrf_failure(request, reason=""):
    return error_response(request, DeskError(403, "CSRF_FAILED", "페이지를 새로 고친 뒤 다시 시도해 주세요."))


class DeskMiddleware(MiddlewareMixin):
    def process_exception(self, request, exception):
        if isinstance(exception, DeskError):
            return error_response(request, exception)
        if isinstance(exception, DatabaseError):
            logger.error("Database request failed (%s).", type(exception).__name__)
            return error_response(request, DeskError(503, "SERVICE_UNAVAILABLE", "데이터를 조회할 수 없습니다. 잠시 후 다시 시도해 주세요."))

    def process_response(self, request, response):
        response["Cache-Control"] = "no-store"
        return response
