from django.urls import path

from . import views


app_name = "followup"
urlpatterns = [
    path("follow-up/", views.page, name="page"),
    path("follow-up/register/", views.register, name="register"),
    path("follow-up/tracks/<uuid:ticket_uuid>/remove/", views.remove, name="remove"),
    path("follow-up/contacts/<uuid:contact_uuid>/complete/", views.finish, name="finish"),
    path("follow-up/chats/<uuid:chat_uuid>/", views.chat_page, name="chat-page"),
    path("api/follow-up/tracks", views.tracks, name="tracks"),
    path("api/follow-up/tracks/<uuid:ticket_uuid>", views.track, name="track"),
    path("api/follow-up/notifications", views.notifications, name="notifications"),
    path("api/follow-up/contacts", views.contacts, name="contacts"),
    path("api/follow-up/contacts/<uuid:contact_uuid>", views.contact, name="contact"),
    path("api/follow-up/contacts/<uuid:contact_uuid>/complete", views.complete, name="complete"),
    path("api/chats/<uuid:chat_uuid>", views.chat, name="chat"),
]
