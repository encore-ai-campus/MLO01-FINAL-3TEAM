import json

from django.conf import settings
from django.db import connection, transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.crypto import constant_time_compare
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.presentation.views import session_token
from apps.accounts.repository import company_staff
from apps.accounts.services import current_actor, require_settings_admin, settings_employee
from apps.tickets.actions import require_assignee, require_conduct
from config.database import read_snapshot
from config.http import DeskError, validate_form

from .. import repository as repo
from ..services import apply_category_result, change_analysis, get_policy, save_policy
from .forms import AnalysisChangeForm, AssignmentPolicyForm, CategoryChangeForm, CategoryResultForm, CategoryRouteForm


@require_GET
def assignment(request):
    with connection.cursor() as cursor:
        actor = current_actor(session_token(request), cursor)
        require_settings_admin(actor)
        staff = company_staff(cursor, actor["company_uuid"])
    return render(request, "assignment/employees.html", {
        "actor": actor, "active": "settings", "title": "자동 배정 설정", "staff": staff,
        "employee_url": "assignment:policy",
    })


@require_http_methods(["GET", "POST"])
def policy(request, staff_uuid):
    saving = request.method == "POST"
    with transaction.atomic(), connection.cursor() as cursor:
        actor = current_actor(session_token(request), cursor)
        require_settings_admin(actor)
        staff = settings_employee(cursor, actor, staff_uuid, lock=saving)
        company = actor["company_uuid"]
        form = AssignmentPolicyForm(request.POST if saving else None, initial=get_policy(cursor, company, staff_uuid))
        if saving and form.is_valid():
            save_policy(cursor, company, staff_uuid, form.cleaned_data)
            return redirect("assignment:policy", staff_uuid=staff_uuid)
    return render(request, "preferences/form.html", {
        "actor": actor, "active": "settings", "title": "자동 배정 설정", "subject": staff["name"],
        "form": form, "back_url": "assignment:index", "batch_sizes": (1, 5, 10),
    }, status=400 if saving else 200)


@require_http_methods(["GET", "POST"])
def category_routes(request):
    with transaction.atomic(), connection.cursor() as cursor:
        actor = current_actor(session_token(request), cursor)
        require_settings_admin(actor)
        company_uuid = actor["company_uuid"]
        categories, teams = repo.routing_options(cursor, company_uuid)
        if request.method == "POST":
            data = validate_form(CategoryRouteForm, request)
            if data["category_uuid"] not in {row["category_uuid"] for row in categories}:
                raise DeskError(422, "INVALID_CATEGORY", "회사 도메인의 카테고리를 선택해 주세요.")
            if data["team_uuid"] and data["team_uuid"] not in {row["team_uuid"] for row in teams}:
                raise DeskError(422, "INVALID_TEAM", "소속 회사의 부서를 선택해 주세요.")
            repo.save_route(cursor, company_uuid, **data)
            return redirect("assignment:category-routes")
    return render(request, "assignment/category_routes.html", {
        "actor": actor, "active": "settings", "categories": categories, "teams": teams,
    })


@require_http_methods(["GET", "POST"])
def category(request, ticket_uuid, as_json=False):
    token = session_token(request)
    if request.method == "POST":
        form = CategoryChangeForm if as_json else AnalysisChangeForm
        result = change_analysis(token, ticket_uuid, **validate_form(form, request))
        if as_json:
            return JsonResponse(result)
        url = reverse("tickets:detail", args=(ticket_uuid,))
        return redirect(f"{url}?{request.GET.urlencode()}" if request.GET else url)
    with read_snapshot() as cursor:
        actor = current_actor(token, cursor)
        require_conduct(actor, cursor)
        ticket = repo.read_ticket(cursor, actor["company_uuid"], ticket_uuid)
        if ticket is None:
            raise DeskError(404, "TICKET_NOT_FOUND", "티켓을 찾을 수 없습니다.")
        require_assignee(actor, ticket)
        categories = repo.category_choices(cursor, ticket["domain_uuid"])
        cursor.execute("SELECT voc.ticket_feature_uuid(%s)", (ticket_uuid,))
        ticket["feature_uuid"] = cursor.fetchone()[0]
        if as_json:
            return JsonResponse({"current_category_uuid": ticket["category_uuid"], "categories": categories,
                                 "feature_uuid": str(ticket["feature_uuid"]) if ticket["feature_uuid"] else None})
        teams = repo.team_choices(cursor, actor["company_uuid"])
    ticket["category"] = next((category["name"] for category in categories
                               if category["category_uuid"] == ticket["category_uuid"]), None)
    current_team = next((team["name"] for team in teams if team["team_uuid"] == ticket["assigned_team_uuid"]), "미배정")
    return render(request, "assignment/category_form.html", {
        "actor": actor, "active": "tickets", "ticket": ticket, "categories": categories,
        "sentiments": AnalysisChangeForm.base_fields["sentiment"].choices, "teams": teams, "current_team": current_team,
    })


@csrf_exempt
@require_POST
def category_result(request):
    configured_token = settings.VOC_ASSIGNMENT_WORKER_TOKEN
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if not configured_token:
        raise DeskError(503, "ASSIGNMENT_WORKER_NOT_CONFIGURED", "분류 워커 연결이 설정되지 않았습니다.")
    if scheme.lower() != "bearer" or not constant_time_compare(token, configured_token):
        raise DeskError(403, "ASSIGNMENT_WORKER_FORBIDDEN", "분류 워커 인증이 필요합니다.")
    if request.content_type != "application/json":
        raise DeskError(415, "INVALID_CONTENT_TYPE", "JSON 요청 본문이 필요합니다.")
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        data = None
    if not isinstance(data, dict) or "category_uuid" not in data:
        raise DeskError(422, "INVALID_CATEGORY_RESULT", "카테고리 UUID 또는 null을 보내 주세요.")
    category_uuid = data["category_uuid"]
    if category_uuid is not None and (not isinstance(category_uuid, str) or not category_uuid.strip()):
        raise DeskError(422, "INVALID_CATEGORY_RESULT", "카테고리 UUID 또는 null을 보내 주세요.")
    form = CategoryResultForm(data)
    if not form.is_valid():
        raise DeskError(422, "INVALID_REQUEST", "분류 결과의 식별자와 본문 버전을 확인해 주세요.")
    return JsonResponse(apply_category_result(**form.cleaned_data))
