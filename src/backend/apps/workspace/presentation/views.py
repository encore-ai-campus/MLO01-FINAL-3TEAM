from django.shortcuts import render
from django.views.decorators.http import require_GET

from apps.accounts.presentation.views import session_token
from apps.accounts.services import current_actor


def render_page(request, template_name, active, context=None):
    actor = current_actor(session_token(request))
    return render(request, template_name, {"actor": actor, "active": active, **(context or {})})


@require_GET
def home(request):
    return render_page(request, "workspace/home.html", "home")
