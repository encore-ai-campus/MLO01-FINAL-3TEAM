from django.urls import path

from . import views

app_name = "tickets"
urlpatterns = [
    path("tickets/", views.ticket_list, name="list"),
    path("tickets/<uuid:ticket_id>/", views.detail, name="detail"),
    path("tickets/<uuid:ticket_id>/complete/", views.complete_ticket, name="complete"),
    path("tickets/<uuid:ticket_id>/assign/", views.assign_ticket, name="assign"),
    path("api/tickets/options", views.options),
    path("tickets/<uuid:ticket_id>/attachments/<uuid:attachment_id>/",
         views.message_attachment, name="message-attachment"),
    path("api/tickets", views.ticket_list, {"as_json": True}),
    path("api/tickets/<uuid:ticket_id>/memos", views.add_memo, name="add-memo"),
    path("api/tickets/<uuid:ticket_id>/summary", views.ticket_summary, name="summary"),
    path("api/tickets/<str:ticket_id>", views.detail, {"as_json": True}),
    path("api/ticket-requests", views.ticket_requests),
    path("api/ticket-requests/<uuid:action_uuid>/attachment", views.request_attachment, name="request-attachment"),
    path("api/ticket-requests/<uuid:action_uuid>/attachments/<uuid:attachment_id>",
         views.request_attachment, name="request-attachment-file"),
    path("api/tickets/<uuid:ticket_id>/transfer/<uuid:request_uuid>/accept",
         views.accept_transfer, name="accept-transfer"),
]

for action in ("messages", "transfer", "review-requests"):
    urlpatterns += [
        path(f"tickets/<uuid:ticket_id>/{action}/", views.ticket_action, {"action": action}, name=action),
        path(f"api/tickets/<uuid:ticket_id>/{action}", views.ticket_action, {"action": action, "as_json": True}),
    ]

for control in ("quick-replies", "collaboration-options"):
    urlpatterns.append(path(f"api/tickets/<uuid:ticket_id>/{control}", views.detail_controls, {"control": control}))
