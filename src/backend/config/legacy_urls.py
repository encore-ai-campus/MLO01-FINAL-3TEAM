from django.urls import path
from django.views.generic import TemplateView

# These published file paths remain valid for earlier bookmarks.
pages = (
    ("01-LoginPage", "login", "/login/"),
    ("02-WorkHomePage", "home", "/home/"),
    ("03-TicketListPage", "tickets", "/tickets/"),
    ("04-TicketDetailPage", "detail", "/tickets/"),
    ("05-InboxPage", "inbox", "/inbox/"),
    ("06-AnalyticsDashboardPage", "analytics", "/analytics/"),
)

urlpatterns = [
    path(
        f"{filename}/{filename}.html",
        TemplateView.as_view(template_name="legacy_launcher.html", extra_context={"page": page, "target": url}),
    )
    for filename, page, url in pages
]
