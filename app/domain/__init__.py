"""Broker-agnostic domain models shared by services, pages, and tests.

Nothing here knows about IOL. The adapter in ``app.infrastructure.iol``
translates broker responses into these models so a second broker can be added
without touching the pages.
"""
