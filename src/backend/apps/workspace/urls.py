from django.urls import path
from apps.analytics.presentation.views import analytics
from apps.collaboration.presentation import views as collaboration
from .presentation.views import home

app_name = "workspace"
urlpatterns = [
    path("home/", home, name="home"),
    path("inbox/", collaboration.inbox, name="inbox"),
    path("inbox/<uuid:request_id>/", collaboration.inbox, name="request"),
    path("api/inbox/<str:kind>/<uuid:item_id>/read/", collaboration.read, name="inbox-read"),
    path("api/inbox/requests/<uuid:request_id>/reply/", collaboration.reply, name="inbox-reply"),
    path("analytics/", analytics, name="analytics"),
]
