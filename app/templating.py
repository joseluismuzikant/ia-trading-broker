"""Shared Jinja2 template environment."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app import __version__
from app.config import get_settings

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Exposed as a callable so tests that patch the environment are respected.
templates.env.globals["app_version"] = __version__
templates.env.globals["settings"] = get_settings

