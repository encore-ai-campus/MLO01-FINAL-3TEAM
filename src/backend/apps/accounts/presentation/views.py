import json
from urllib.parse import urlencode

from django.conf import settings
from django.db import connection, transaction
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import rotate_token
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from config.http import DeskError
from .. import repository, services
from ..repository import SESSION_SECONDS
from .forms import WorkPermissionsForm, credentials

# Existing API clients retain their /api cookie. The desk token covers server-rendered pages.
COOKIE_PATHS = {"voc_desk_session": "/", "voc_session": "/api"}


def session_token(request):
    return request.COOKIES.get("voc_desk_session") or request.COOKIES.get("voc_session")


def authenticated_response(request, response, token):
    rotate_token(request)
    for name, path in COOKIE_PATHS.items():
        response.set_cookie(name, token, max_age=SESSION_SECONDS, path=path, httponly=True,
                            secure=request.is_secure() or settings.SESSION_COOKIE_SECURE, samesite="Lax")
    return response


def clear_session(request, response):
    for token in {request.COOKIES.get(name) for name in COOKIE_PATHS}:
        services.logout(token)
    for name, path in COOKIE_PATHS.items():
        response.delete_cookie(name, path=path, samesite="Lax")
    return response


@require_POST
def api_login(request):
    try:
        body = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        body = None
    email, password = credentials(body if isinstance(body, dict) else {})
    return authenticated_response(request, JsonResponse({"email": email}), services.login(email, password))


@require_GET
def me(request):
    actor = services.current_actor(session_token(request))
    return JsonResponse({"email": actor["email"], "name": actor["name"]})


@require_POST
def api_logout(request):
    return clear_session(request, HttpResponse(status=204))


@require_GET
def resume(request):
    # A cookie issued by the previous desk at Path=/api reaches this URL.
    dest = safe_destination(request)
    legacy_token = request.COOKIES.get("voc_session")
    if legacy_token:
        try:
            services.current_actor(legacy_token)
        except DeskError as error:
            if error.code != "LOGIN_REQUIRED":
                raise
        else:
            response = redirect(dest)
            response.set_cookie("voc_desk_session", legacy_token, max_age=SESSION_SECONDS, path="/",
                                httponly=True, secure=request.is_secure() or settings.SESSION_COOKIE_SECURE,
                                samesite="Lax")
            return response
    return redirect(reverse("accounts:login") + "?" + urlencode({"next": dest}))


def safe_destination(request):
    dest = request.POST.get("next") or request.GET.get("next")
    if dest and url_has_allowed_host_and_scheme(dest, {request.get_host()}, require_https=request.is_secure()):
        return dest
    return reverse("tickets:list")


@require_http_methods(["GET", "POST"])
@ensure_csrf_cookie
def login(request):
    dest = safe_destination(request)
    if request.method == "POST":
        try:
            email, password = credentials(request.POST)
            token = services.login(email, password)
        except DeskError as error:
            return render(request, "accounts/login.html", {
                "email": request.POST.get("email", ""), "next": dest,
                "error": error.message, "error_code": error.code,
            }, status=error.status)
        return authenticated_response(request, redirect(dest), token)
    if session_token(request):
        try:
            services.current_actor(session_token(request))
        except DeskError as error:
            if error.code != "LOGIN_REQUIRED":
                raise
        else:
            return redirect(dest)
    return render(request, "accounts/login.html", {"next": dest})


@require_POST
def logout(request):
    return clear_session(request, redirect("accounts:login"))


@require_GET
def permissions(request):
    with connection.cursor() as cursor:
        actor = services.current_actor(session_token(request), cursor)
        services.require_settings_admin(actor)
        staff = repository.company_staff(cursor, actor["company_uuid"])
    return render(request, "preferences/employees.html", {
        "actor": actor, "active": "settings", "title": "업무 권한 설정", "staff": staff,
        "employee_url": "accounts:employee-permissions",
    })


@require_http_methods(["GET", "POST"])
def employee_permissions(request, staff_uuid):
    saving = request.method == "POST"
    with transaction.atomic(), connection.cursor() as cursor:
        actor = services.current_actor(session_token(request), cursor)
        services.require_settings_admin(actor)
        company = actor["company_uuid"]
        staff = services.settings_employee(cursor, actor, staff_uuid, lock=saving)
        read_only = staff["role"] == "super_admin" and actor["role"] != "super_admin"
        if saving and read_only:
            raise DeskError(403, "PERMISSION_TARGET_FORBIDDEN", "최고관리자의 업무 권한은 최고관리자만 변경할 수 있습니다.")
        codes = services.WORK_PERMISSION_CODES
        granted = repository.work_permissions(cursor, company, staff_uuid, codes)
        form = WorkPermissionsForm(request.POST if saving else None, initial={"permissions": granted})
        form.fields["permissions"].disabled = read_only
        if saving and form.is_valid():
            if not repository.set_work_permissions(cursor, company, staff_uuid, codes, form.cleaned_data["permissions"]):
                raise DeskError(503, "PERMISSIONS_UNAVAILABLE", "업무 권한을 저장할 수 없습니다. 설정 스키마 적용 상태를 확인해 주세요.")
            return redirect("accounts:employee-permissions", staff_uuid=staff_uuid)
    return render(request, "preferences/form.html", {
        "actor": actor, "active": "settings", "title": "업무 권한 설정", "subject": staff["name"],
        "form": form, "back_url": "accounts:permissions", "read_only": read_only,
    }, status=400 if saving else 200)
