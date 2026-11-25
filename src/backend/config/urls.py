from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="tickets:list"), name="index"),
    path("", include("apps.accounts.presentation.urls")),
    path("", include("apps.tickets.presentation.urls")),
    path("src/pages/", include("config.legacy_urls")),
    path("", include("apps.workspace.urls")),
    path("", include("apps.assignment.presentation.urls")),
    path("", include("apps.preferences.presentation.urls")),
    path("", include("apps.followup.presentation.urls")),
]
