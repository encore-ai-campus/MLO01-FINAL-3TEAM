from django.urls import path

from . import views

app_name = "assignment"
urlpatterns = [
    path("settings/assignment/", views.assignment, name="index"),
    path("settings/assignment/<uuid:staff_uuid>/", views.policy, name="policy"),
]
