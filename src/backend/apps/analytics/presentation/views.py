from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.accounts.presentation.views import session_token
from apps.accounts.services import DASHBOARD_READ, current_actor, require_work_permission
from config.database import read_snapshot
from config.http import DeskError

from ..services import analytics_context


@require_GET
def analytics(request):
    token = session_token(request)
    if not token:
        raise DeskError(401, "LOGIN_REQUIRED", "로그인해 주세요.")
    with read_snapshot() as cursor:
        actor = current_actor(token, cursor)
        require_work_permission(cursor, actor, DASHBOARD_READ)
        context = analytics_context(cursor, actor, request.GET)
    return render(request, "analytics/analytics.html", {"actor": actor, "active": "analytics", **context})
