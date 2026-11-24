from django.views.decorators.http import require_GET

from apps.workspace.presentation.views import render_page
from ..services import analytics_context


@require_GET
def analytics(request):
    return render_page(request, "analytics/analytics.html", "analytics", analytics_context(request.GET))
