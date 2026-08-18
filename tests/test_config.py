"""Configuration loading: validation, provider gating and env overrides."""

from __future__ import annotations

import pytest

from miami_bot.config import ConfigError, Settings


def test_criteria_load_from_yaml(settings):
    search = settings.search
    assert "Bal Harbour" in search.cities
    assert search.min_price == 5000 and search.max_price == 12000
    assert search.min_beds == 2 and search.min_baths == 2 and search.min_sqft == 1200
    assert search.min_lease_months == 6 and search.max_lease_months == 12
    assert "short term" in search.excluded_keywords
    assert "vacation" in search.excluded_keywords
    assert "airbnb" in search.excluded_keywords
    assert search.building.min_amenity_matches >= 1


def test_providers_are_off_without_credentials(settings):
    status = settings.provider_status()
    assert status["rapidapi"] is False
    assert status["scrapingbee"] is False
    assert status["county_assessor"] is True      # public data, no key needed


def test_missing_credentials_produce_actionable_warnings(settings):
    warnings = " ".join(settings.warnings())
    assert "RAPIDAPI_KEY" in warnings
    assert "SCRAPINGBEE_API_KEY" in warnings
    assert "alert channel" in warnings


def test_env_variables_configure_providers(monkeypatch, tmp_path):
    monkeypatch.setenv("RAPIDAPI_KEY", "abc")
    monkeypatch.setenv("RAPIDAPI_ENABLED_SOURCES", "zillow,redfin")
    monkeypatch.setenv("SCRAPINGBEE_API_KEY", "sb")
    monkeypatch.setenv("SCRAPINGBEE_STEALTH_PROXY", "true")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "bot@example.com")
    monkeypatch.setenv("SMTP_TO", "me@example.com, you@example.com")

    settings = Settings.load("config.yaml", env_file=None)
    assert settings.rapidapi.enabled
    assert settings.rapidapi.enabled_sources == ["zillow", "redfin"]
    assert settings.scrapingbee.stealth_proxy is True
    assert settings.alerts.smtp_to == ["me@example.com", "you@example.com"]
    assert settings.alerts.email_enabled


def test_an_invalid_auth_style_is_rejected(monkeypatch):
    monkeypatch.setenv("REALTYAPI_AUTH_STYLE", "magic")
    with pytest.raises(ConfigError):
        Settings.load("config.yaml", env_file=None)


def test_an_invalid_smtp_security_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("SMTP_SECURITY", "carrier-pigeon")
    with pytest.raises(ConfigError):
        Settings.load("config.yaml", env_file=None)


def test_a_malformed_numeric_env_var_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("HTTP_TIMEOUT_SECONDS", "soon")
    assert Settings.load("config.yaml", env_file=None).http.timeout == 30.0


def test_a_missing_config_file_is_reported_clearly():
    with pytest.raises(ConfigError, match="not found"):
        Settings.load("does-not-exist.yaml", env_file=None)


def test_contradictory_criteria_are_rejected(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        "search:\n"
        "  cities: [Miami Beach]\n"
        "  min_price: 12000\n"
        "  max_price: 5000\n"
    )
    with pytest.raises(ConfigError, match="min_price"):
        Settings.load(path, env_file=None)


def test_an_impossible_lease_window_is_rejected(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        "search:\n"
        "  cities: [Miami Beach]\n"
        "  min_lease_months: 12\n"
        "  max_lease_months: 6\n"
    )
    with pytest.raises(ConfigError, match="lease"):
        Settings.load(path, env_file=None)


def test_malformed_yaml_is_reported_clearly(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("search: [unclosed\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        Settings.load(path, env_file=None)


def test_required_amenities_load_from_yaml(settings):
    assert settings.search.building.required_amenities == ["ocean_view"]


def test_a_typo_in_required_amenities_is_caught_at_load_time(tmp_path):
    """A misspelled category would silently reject every listing forever."""
    path = tmp_path / "typo.yaml"
    path.write_text(
        "search:\n"
        "  cities: [Miami Beach]\n"
        "  building:\n"
        "    luxury_amenities:\n"
        "      ocean_view: [ocean view]\n"
        "    required_amenities: [oceanview]\n"
    )
    with pytest.raises(ConfigError, match="unknown category"):
        Settings.load(path, env_file=None)
