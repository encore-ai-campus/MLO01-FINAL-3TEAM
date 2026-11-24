from django.http import Http404
from django.views.decorators.http import require_GET

from apps.accounts.presentation.views import session_token
from apps.tickets.actions import received_requests
from apps.workspace.presentation.views import render_page
from config.http import DeskError
from ..repository import preview_data
from ..services import inbox_context


@require_GET
def inbox(request, request_id=None):
    context = inbox_context(request.GET, request_id)
    token = session_token(request)
    if token and context["tab"] in ("transfer", "review") and not context["empty_demo"]:
        try:
            rows = received_requests(token, context["tab"])
        except DeskError as error:
            if error.code != "TICKET_CONDUCT_FORBIDDEN":
                raise
        else:
            query = context["query"].casefold()
            context["received_ticket_requests"] = [
                row for row in rows if any(query in value.casefold()
                for value in (row["title"], "#" + row["ticket_number"], row["sender"]))
            ]
    return render_page(request, "collaboration/inbox.html", "inbox", context)


@require_GET
def sample_ticket(request, number):
    data = preview_data()
    rows = [row for group in data["requestGroups"].values() for row in group if row["id"] == str(number)]
    if not rows:
        raise Http404
    return render_page(request, "collaboration/sample_ticket.html", "inbox", {"row": rows[0], "requests": rows,
                "profile": data["ticketProfiles"][str(number)]})
