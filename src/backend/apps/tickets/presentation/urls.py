from django.urls import path

from . import views

app_name = "tickets"
urlpatterns = [
    path("tickets/", views.ticket_list, name="list"),
    path("tickets/<uuid:ticket_uuid>/", views.detail, name="detail"),
    path("api/tickets/options", views.options),
    path("api/tickets", views.ticket_list, {"as_json": True}),
    path("api/tickets/<str:ticket_uuid>", views.detail, {"as_json": True}),
]
