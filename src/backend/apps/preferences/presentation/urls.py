from django.urls import path

from . import views

app_name = "preferences"
urlpatterns = [
    path("settings/", views.index, name="index"),
    path("settings/display/", views.display, name="display"),
    path("settings/display/cards/", views.home_cards, name="home-cards"),
    path("settings/replies/", views.replies, name="replies"),
    path("settings/replies/new/", views.reply, name="reply-create"),
    path("settings/replies/<uuid:reply_uuid>/", views.reply, name="reply-edit"),
    path("settings/messenger/", views.messenger, name="messenger"),
]
