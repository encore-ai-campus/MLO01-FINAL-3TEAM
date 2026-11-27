from django.urls import path

from . import model_operations, training, views

app_name = "preferences"
urlpatterns = [
    path("settings/", views.index, name="index"),
    path("settings/display/", views.display, name="display"),
    path("settings/display/cards/", views.home_cards, name="home-cards"),
    path("settings/replies/", views.replies, name="replies"),
    path("settings/replies/new/", views.reply, name="reply-create"),
    path("settings/replies/<uuid:reply_uuid>/", views.reply, name="reply-edit"),
    path("settings/messenger/", views.messenger, name="messenger"),
    path("model-training/", training.page, name="model-training"),
    path("model-training/schedule/", model_operations.schedule, name="retraining-schedule"),
    path("model-training/review/", model_operations.review, name="model-review"),
    path("model-training/runs/", training.recent, name="training-recent"),
    path("model-training/runs/<str:workflow_name>/", training.detail, name="training-detail"),
    path("model-training/runs/<str:workflow_name>/status/", training.status, name="training-status"),
    path("model-training/runs/<str:workflow_name>/stop/", training.stop, name="training-stop"),
    path("settings/model-training/", training.page, name="legacy-model-training"),
]
