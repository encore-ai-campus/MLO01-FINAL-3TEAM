from django.urls import path
from . import views

app_name = "accounts"
urlpatterns = [
    path("login/", views.login, name="login"),
    path("logout/", views.logout, name="logout"),
    path("api/auth/login", views.api_login),
    path("api/auth/resume", views.resume, name="resume"),
    path("api/auth/me", views.me),
    path("api/auth/logout", views.api_logout),
]
