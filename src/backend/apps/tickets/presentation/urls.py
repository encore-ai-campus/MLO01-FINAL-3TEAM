from django.urls import path

from . import views

app_name = "tickets"
urlpatterns = [
    path("monitoring/readiness", views.monitoring_readiness),
    path("tickets/", views.ticket_list, name="list"),
    path("tickets/<uuid:ticket_uuid>/", views.detail, name="detail"),
    path("tickets/<uuid:ticket_uuid>/classification/", views.update_ticket, name="classification"),
    path("tickets/<uuid:ticket_uuid>/reply/", views.send_reply, name="reply"),
    path("tickets/<uuid:ticket_uuid>/complete/", views.update_ticket, {"complete": True}, name="complete"),
    path("api/tickets/predictions/displayed", views.prediction_display, name="prediction-display"),
    path("api/tickets/options", views.options),
    path("api/tickets", views.ticket_list, {"as_json": True}),
    path("api/tickets/<uuid:ticket_uuid>/memos", views.add_memo, name="add-memo"),
    path("api/tickets/<str:ticket_uuid>", views.detail, {"as_json": True}),
]
