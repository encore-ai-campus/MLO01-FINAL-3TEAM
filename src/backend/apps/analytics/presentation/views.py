from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.accounts.presentation.views import session_token
from apps.accounts.services import current_actor
from config.database import read_snapshot
from config.http import DeskError

from ..repository import staff_role
from ..services import analytics_context


@require_GET
def analytics(request):
    token = session_token(request)
    if not token:
        raise DeskError(401, "LOGIN_REQUIRED", "로그인해 주세요.")
    with read_snapshot() as cursor:
        actor = current_actor(token, cursor)
        if staff_role(cursor, actor) != "admin":
            raise DeskError(403, "ANALYTICS_FORBIDDEN", "관리자만 분석 대시보드를 조회할 수 있습니다.")
        context = analytics_context(cursor, actor, request.GET)
    return render(request, "analytics/analytics.html", {"actor": actor, "active": "analytics", **context})
