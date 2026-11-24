from django.urls import path
from apps.analytics.presentation.views import analytics
from apps.collaboration.presentation.views import inbox, sample_ticket
from .presentation.views import home

app_name = "workspace"
urlpatterns = [
    path("home/", home, name="home"),
    path("inbox/", inbox, name="inbox"),
    path("inbox/sample-ticket/<int:number>/", sample_ticket, name="sample-ticket"),
    path("inbox/<slug:request_id>/", inbox, name="request"),
    path("analytics/", analytics, name="analytics"),
]
