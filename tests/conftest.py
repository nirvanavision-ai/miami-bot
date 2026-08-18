"""Shared fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from miami_bot.config import Settings  # noqa: E402
from miami_bot.db import Database  # noqa: E402
from miami_bot.models import Listing  # noqa: E402
from miami_bot.util.http import HttpClient  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    """Settings loaded from the repository's own config.yaml, no .env."""
    return Settings.load(ROOT / "config.yaml", env_file=None)


@pytest.fixture
def criteria(settings: Settings):
    return settings.search


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture
def http() -> HttpClient:
    # rate_limit 0 so tests never sleep.
    return HttpClient(rate_limit_seconds=0.0, max_retries=0)


def make_listing(**overrides) -> Listing:
    """A listing that passes every hard constraint unless overridden."""
    base = {
        "source": "test",
        "source_id": "t1",
        "url": "https://example.com/listing/1",
        "address": "9705 Collins Ave #1502N, Bal Harbour, FL 33154",
        "beds": 2.0,
        "baths": 2.5,
        "sqft": 1450,
        "year_built": 2018,
        "price": 9500,
        "latitude": 25.8890,
        "longitude": -80.1233,
        "property_type": "Condo",
        "title": "Oceanfront 2BR",
        "description": (
            "Annual lease, 6-12 months. Valet parking, concierge, "
            "oceanfront pool, spa and fitness center."
        ),
        "photos": ["https://example.com/photo.jpg"],
    }
    base.update(overrides)
    return Listing(**base)


@pytest.fixture
def listing() -> Listing:
    return make_listing()
