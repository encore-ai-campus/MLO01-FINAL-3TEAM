from django.urls import path

from . import views

app_name = "assignment"
urlpatterns = [
    path("settings/assignment/", views.assignment, name="index"),
    path("settings/assignment/category-routes/", views.category_routes, name="category-routes"),
    path("settings/assignment/<uuid:staff_uuid>/", views.policy, name="policy"),
    path("tickets/<uuid:ticket_uuid>/category/", views.category, name="ticket-category"),
    path("api/tickets/<uuid:ticket_uuid>/category", views.category, {"as_json": True}),
    path("api/internal/ticket-assignments/category-result", views.category_result, name="category-result"),
]
