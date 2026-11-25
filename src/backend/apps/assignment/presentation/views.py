from django.db import connection, transaction
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from apps.accounts.presentation.views import session_token
from apps.accounts.repository import company_staff
from apps.accounts.services import current_actor, require_settings_admin, settings_employee

from ..services import get_policy, save_policy
from .forms import AssignmentPolicyForm


@require_GET
def assignment(request):
    with connection.cursor() as cursor:
        actor = current_actor(session_token(request), cursor)
        require_settings_admin(actor)
        staff = company_staff(cursor, actor["company_uuid"])
    return render(request, "preferences/employees.html", {
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
