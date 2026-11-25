from django.urls import path

from . import views

app_name = "tickets"
urlpatterns = [
    path("tickets/", views.ticket_list, name="list"),
    path("tickets/<uuid:ticket_uuid>/", views.detail, name="detail"),
    path("api/tickets/options", views.options),
    path("api/tickets", views.ticket_list, {"as_json": True}),
    path("api/tickets/<uuid:ticket_uuid>/memos", views.add_memo, name="add-memo"),
    path("api/tickets/<str:ticket_uuid>", views.detail, {"as_json": True}),
    path("api/ticket-requests", views.ticket_requests),
    path("api/ticket-requests/<uuid:action_uuid>/attachment", views.request_attachment, name="request-attachment"),
    path("api/tickets/<uuid:ticket_uuid>/transfer/<uuid:request_uuid>/accept",
         views.accept_transfer, name="accept-transfer"),
]

for action in ("messages", "transfer", "review-requests"):
    urlpatterns += [
        path(f"tickets/<uuid:ticket_uuid>/{action}/", views.ticket_action, {"action": action}, name=action),
        path(f"api/tickets/<uuid:ticket_uuid>/{action}", views.ticket_action, {"action": action, "as_json": True}),
    ]

for control in ("quick-replies", "collaboration-options"):
    urlpatterns.append(path(f"api/tickets/<uuid:ticket_uuid>/{control}", views.detail_controls, {"control": control}))
