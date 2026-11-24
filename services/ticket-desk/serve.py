"""Serve the compiled ticket UI and session-bound API from one origin."""
import os
from importlib import import_module
from pathlib import Path

from fastapi.staticfiles import StaticFiles


def create_app():
    password_file = Path("/run/secrets/postgres_password")
    if password_file.is_file():
        os.environ["PGPASSWORD"] = password_file.read_text().strip()
    app = import_module("src.pages.01-LoginPage.backend.main").create_app()
    app.mount("/", StaticFiles(directory="/app/static", html=True), name="desk")
    return app
