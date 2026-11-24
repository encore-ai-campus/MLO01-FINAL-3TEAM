from django.http import Http404
from django.views.decorators.http import require_GET

from apps.workspace.presentation.views import render_page
from ..repository import preview_data
from ..services import inbox_context


@require_GET
def inbox(request, request_id=None):
    return render_page(request, "collaboration/inbox.html", "inbox", inbox_context(request.GET, request_id))


@require_GET
def sample_ticket(request, number):
    data = preview_data()
    rows = [row for group in data["requestGroups"].values() for row in group if row["id"] == str(number)]
    if not rows:
        raise Http404
    return render_page(request, "collaboration/sample_ticket.html", "inbox", {"row": rows[0], "requests": rows,
                "profile": data["ticketProfiles"][str(number)]})
