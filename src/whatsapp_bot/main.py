"""ASGI factory for ``uvicorn whatsapp_bot.main:create_application --factory``."""

from .api import create_app


def create_application():
    return create_app()
