"""Settings: environment variables (secrets, endpoints) + YAML (search criteria).

Secrets never live in YAML and criteria never live in code. Loading is strict
about types but permissive about absence -- a missing provider key disables that
provider rather than crashing the run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:  # python-dotenv is optional at runtime
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:  # type: ignore[misc]
        return False

from .util.logging import get_logger

log = get_logger(__name__)


class ConfigError(RuntimeError):
    """Raised when configuration is present but invalid."""


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------
def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        log.warning("%s=%r is not an integer; using %s", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        log.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = _env(name)
    if not raw:
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Provider credential blocks
# ---------------------------------------------------------------------------
@dataclass
class RapidApiSettings:
    api_key: str = ""
    zillow_host: str = "zillow-com1.p.rapidapi.com"
    realtor_host: str = "realty-in-us.p.rapidapi.com"
    redfin_host: str = "redfin-com-data.p.rapidapi.com"
    enabled_sources: list[str] = field(default_factory=lambda: ["zillow", "realtor"])
    #: Endpoint paths for the Zillow wrapper. Overridable because the RapidAPI
    #: marketplace carries several competing Zillow wrappers (Axesso and others)
    #: that expose the same data under different paths. Point these at whatever
    #: your subscription documents and the tolerant field maps do the rest.
    zillow_search_path: str = "/propertyExtendedSearch"
    zillow_detail_path: str = "/property"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_env(cls) -> RapidApiSettings:
        return cls(
            api_key=_env("RAPIDAPI_KEY"),
            zillow_host=_env("RAPIDAPI_ZILLOW_HOST", "zillow-com1.p.rapidapi.com"),
            realtor_host=_env("RAPIDAPI_REALTOR_HOST", "realty-in-us.p.rapidapi.com"),
            redfin_host=_env("RAPIDAPI_REDFIN_HOST", "redfin-com-data.p.rapidapi.com"),
            enabled_sources=[
                s.lower() for s in _env_list("RAPIDAPI_ENABLED_SOURCES", ["zillow", "realtor"])
            ],
            zillow_search_path=_env("RAPIDAPI_ZILLOW_SEARCH_PATH", "/propertyExtendedSearch"),
            zillow_detail_path=_env("RAPIDAPI_ZILLOW_DETAIL_PATH", "/property"),
        )


@dataclass
class RealtyApiSettings:
    api_key: str = ""
    base_url: str = "https://api.realtyapi.io/v1"
    auth_style: str = "header_x_api_key"     # header_x_api_key|header_bearer|query_param
    auth_param: str = "api_key"
    search_path: str = "/properties/rentals"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.base_url)

    @classmethod
    def from_env(cls) -> RealtyApiSettings:
        style = _env("REALTYAPI_AUTH_STYLE", "header_x_api_key").lower()
        if style not in {"header_x_api_key", "header_bearer", "query_param"}:
            raise ConfigError(f"REALTYAPI_AUTH_STYLE={style!r} is not a supported auth style")
        return cls(
            api_key=_env("REALTYAPI_KEY"),
            base_url=_env("REALTYAPI_BASE_URL", "https://api.realtyapi.io/v1").rstrip("/"),
            auth_style=style,
            auth_param=_env("REALTYAPI_AUTH_PARAM", "api_key"),
            search_path=_env("REALTYAPI_SEARCH_PATH", "/properties/rentals"),
        )


@dataclass
class ScrapingBeeSettings:
    api_key: str = ""
    premium_proxy: bool = True
    stealth_proxy: bool = False
    render_js: bool = True
    country_code: str = "us"
    wait_ms: int = 4000
    max_requests_per_run: int = 12

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_env(cls) -> ScrapingBeeSettings:
        return cls(
            api_key=_env("SCRAPINGBEE_API_KEY"),
            premium_proxy=_env_bool("SCRAPINGBEE_PREMIUM_PROXY", True),
            stealth_proxy=_env_bool("SCRAPINGBEE_STEALTH_PROXY", False),
            render_js=_env_bool("SCRAPINGBEE_RENDER_JS", True),
            country_code=_env("SCRAPINGBEE_COUNTRY_CODE", "us"),
            wait_ms=_env_int("SCRAPINGBEE_WAIT_MS", 4000),
            max_requests_per_run=_env_int("SCRAPINGBEE_MAX_REQUESTS_PER_RUN", 12),
        )


@dataclass
class RentCastSettings:
    api_key: str = ""
    base_url: str = "https://api.rentcast.io/v1"
    #: Use RentCast's active-listings endpoint as a Module 1 source, not just as
    #: a Module 3 rent AVM. Cheap enough for the free tier: one call per ZIP.
    use_as_listing_source: bool = True

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_env(cls) -> RentCastSettings:
        return cls(
            api_key=_env("RENTCAST_API_KEY"),
            base_url=_env("RENTCAST_BASE_URL", "https://api.rentcast.io/v1").rstrip("/"),
            use_as_listing_source=_env_bool("RENTCAST_AS_LISTING_SOURCE", True),
        )


@dataclass
class CraigslistSettings:
    """Zero-credential RSS source.

    Off by default: the inventory is noisier than the portals and the feed
    publishes no coordinates, so ocean proximity cannot be measured.
    """

    enabled: bool = False
    sites: list[str] = field(default_factory=lambda: ["miami"])

    @classmethod
    def from_env(cls) -> CraigslistSettings:
        return cls(
            enabled=_env_bool("CRAIGSLIST_ENABLED", False),
            sites=_env_list("CRAIGSLIST_SITES", ["miami"]),
        )


@dataclass
class HouseCanarySettings:
    api_key: str = ""
    api_secret: str = ""
    base_url: str = "https://api.housecanary.com/v2"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.api_secret)

    @classmethod
    def from_env(cls) -> HouseCanarySettings:
        return cls(
            api_key=_env("HOUSECANARY_API_KEY"),
            api_secret=_env("HOUSECANARY_API_SECRET"),
            base_url=_env("HOUSECANARY_BASE_URL", "https://api.housecanary.com/v2").rstrip("/"),
        )


@dataclass
class CountySettings:
    """Miami-Dade public data. No credentials required."""

    enabled: bool = True
    pa_proxy_url: str = "https://www.miamidade.gov/Apps/PA/PApublicServiceProxy/PaServicesProxy.ashx"
    gis_url: str = "https://gisweb.miamidade.gov/arcgis/rest/services/MD_Parcels/MapServer/0/query"

    @classmethod
    def from_env(cls) -> CountySettings:
        return cls(
            enabled=_env_bool("MIAMIDADE_ENABLED", True),
            pa_proxy_url=_env(
                "MIAMIDADE_PA_PROXY_URL",
                "https://www.miamidade.gov/Apps/PA/PApublicServiceProxy/PaServicesProxy.ashx",
            ),
            gis_url=_env(
                "MIAMIDADE_GIS_URL",
                "https://gisweb.miamidade.gov/arcgis/rest/services/MD_Parcels/MapServer/0/query",
            ),
        )


@dataclass
class AlertSettings:
    discord_webhook_url: str = ""
    slack_webhook_url: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: list[str] = field(default_factory=list)
    smtp_security: str = "starttls"

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_from and self.smtp_to)

    @property
    def any_enabled(self) -> bool:
        return bool(self.discord_webhook_url or self.slack_webhook_url or self.email_enabled)

    @classmethod
    def from_env(cls) -> AlertSettings:
        security = _env("SMTP_SECURITY", "starttls").lower()
        if security not in {"starttls", "ssl", "none"}:
            raise ConfigError(f"SMTP_SECURITY={security!r} must be starttls, ssl or none")
        return cls(
            discord_webhook_url=_env("DISCORD_WEBHOOK_URL"),
            slack_webhook_url=_env("SLACK_WEBHOOK_URL"),
            smtp_host=_env("SMTP_HOST"),
            smtp_port=_env_int("SMTP_PORT", 587),
            smtp_username=_env("SMTP_USERNAME"),
            smtp_password=_env("SMTP_PASSWORD"),
            smtp_from=_env("SMTP_FROM"),
            smtp_to=_env_list("SMTP_TO"),
            smtp_security=security,
        )


@dataclass
class HttpSettings:
    timeout: float = 30.0
    max_retries: int = 3
    rate_limit_seconds: float = 1.0
    cache_enabled: bool = False
    cache_dir: str = "data/cache"
    cache_ttl_seconds: int = 900

    @classmethod
    def from_env(cls) -> HttpSettings:
        return cls(
            timeout=_env_float("HTTP_TIMEOUT_SECONDS", 30.0),
            max_retries=_env_int("HTTP_MAX_RETRIES", 3),
            rate_limit_seconds=_env_float("HTTP_RATE_LIMIT_SECONDS", 1.0),
            cache_enabled=_env_bool("HTTP_CACHE_ENABLED", False),
            cache_dir=_env("HTTP_CACHE_DIR", "data/cache"),
            cache_ttl_seconds=_env_int("HTTP_CACHE_TTL_SECONDS", 900),
        )


# ---------------------------------------------------------------------------
# YAML-backed search criteria
# ---------------------------------------------------------------------------
@dataclass
class BuildingCriteria:
    min_year_built: int = 2005
    allow_older_if_renovated: bool = True
    renovation_keywords: list[str] = field(default_factory=list)
    min_amenity_matches: int = 3
    luxury_amenities: dict[str, list[str]] = field(default_factory=dict)
    #: Amenity categories that are mandatory, not merely counted. A listing
    #: missing any of these fails outright even if it clears the count.
    required_amenities: list[str] = field(default_factory=list)
    #: When True, a listing whose source published no description is rejected
    #: rather than flagged. Leave False to keep structured feeds usable.
    require_amenity_evidence: bool = False


@dataclass
class SearchCriteria:
    cities: list[str] = field(default_factory=list)
    state: str = "FL"
    zip_codes: list[str] = field(default_factory=list)
    enforce_city_whitelist: bool = True

    max_miles_from_ocean: float = 0.6
    require_coordinates: bool = False

    property_types: list[str] = field(default_factory=list)
    min_beds: float = 2
    min_baths: float = 2
    min_sqft: int = 1200
    require_known_sqft: bool = False

    min_price: int = 5000
    max_price: int = 12000

    min_lease_months: int = 6
    max_lease_months: int = 12
    require_explicit_lease_term: bool = False
    excluded_keywords: list[str] = field(default_factory=list)
    positive_term_keywords: list[str] = field(default_factory=list)

    building: BuildingCriteria = field(default_factory=BuildingCriteria)

    @property
    def normalized_cities(self) -> set[str]:
        return {c.strip().lower() for c in self.cities}

    @property
    def normalized_property_types(self) -> set[str]:
        return {p.strip().lower() for p in self.property_types}

    def validate(self) -> None:
        if self.min_price > self.max_price:
            raise ConfigError("search.min_price cannot exceed search.max_price")
        if self.min_lease_months > self.max_lease_months:
            raise ConfigError("search.min_lease_months cannot exceed search.max_lease_months")
        if self.min_sqft < 0 or self.min_beds < 0 or self.min_baths < 0:
            raise ConfigError("search size constraints must be non-negative")
        if not self.cities and not self.zip_codes:
            raise ConfigError("search needs at least one city or ZIP code")
        if self.max_miles_from_ocean <= 0:
            raise ConfigError("search.max_miles_from_ocean must be positive")

        # A typo in required_amenities would silently reject every listing, so
        # it is caught at load time rather than at 3am with an empty inbox.
        unknown = [
            name for name in self.building.required_amenities
            if name not in self.building.luxury_amenities
        ]
        if unknown:
            known = ", ".join(sorted(self.building.luxury_amenities))
            raise ConfigError(
                f"search.building.required_amenities references unknown "
                f"{'categories' if len(unknown) > 1 else 'category'} "
                f"{', '.join(unknown)}. Known categories: {known}"
            )


@dataclass
class EnrichmentCriteria:
    enabled: bool = True
    only_matched: bool = True
    rentcast: bool = True
    housecanary: bool = False
    county_assessor: bool = True
    overpriced_threshold: float = 1.15
    underpriced_threshold: float = 0.90


@dataclass
class AlertCriteria:
    price_drop_min_dollars: int = 100
    price_drop_min_pct: float = 0.02
    max_alerts_per_run: int = 25
    cooldown_hours: int = 24
    include_warned: bool = True


@dataclass
class PipelineCriteria:
    staleness_min_results: int = 5
    staleness_error_ratio: float = 0.5
    always_run_scraper: bool = False
    enrichment: EnrichmentCriteria = field(default_factory=EnrichmentCriteria)
    alerts: AlertCriteria = field(default_factory=AlertCriteria)


@dataclass
class Settings:
    """Everything the pipeline needs, assembled from env + YAML."""

    search: SearchCriteria
    pipeline: PipelineCriteria
    rapidapi: RapidApiSettings
    realtyapi: RealtyApiSettings
    scrapingbee: ScrapingBeeSettings
    rentcast: RentCastSettings
    craigslist: CraigslistSettings
    housecanary: HouseCanarySettings
    county: CountySettings
    alerts: AlertSettings
    http: HttpSettings

    database_path: str = "data/listings.db"
    log_level: str = "INFO"
    log_file: str = "data/miami_bot.log"
    poll_interval_minutes: int = 60
    dry_run: bool = False

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, config_path: str | Path | None = None, env_file: str | Path | None = ".env") -> Settings:
        if env_file and Path(env_file).exists():
            load_dotenv(env_file, override=False)

        path = Path(config_path or _env("CONFIG_PATH", "config.yaml"))
        search, pipeline = _load_criteria(path)
        search.validate()

        return cls(
            search=search,
            pipeline=pipeline,
            rapidapi=RapidApiSettings.from_env(),
            realtyapi=RealtyApiSettings.from_env(),
            scrapingbee=ScrapingBeeSettings.from_env(),
            rentcast=RentCastSettings.from_env(),
            craigslist=CraigslistSettings.from_env(),
            housecanary=HouseCanarySettings.from_env(),
            county=CountySettings.from_env(),
            alerts=AlertSettings.from_env(),
            http=HttpSettings.from_env(),
            database_path=_env("DATABASE_PATH", "data/listings.db"),
            log_level=_env("LOG_LEVEL", "INFO"),
            log_file=_env("LOG_FILE", "data/miami_bot.log"),
            poll_interval_minutes=_env_int("POLL_INTERVAL_MINUTES", 60),
            dry_run=_env_bool("DRY_RUN", False),
        )

    # --------------------------------------------------------------- reports
    def provider_status(self) -> dict[str, bool]:
        """Which modules will actually run. Printed at startup."""
        return {
            "rapidapi": self.rapidapi.enabled,
            "realtyapi": self.realtyapi.enabled,
            "scrapingbee": self.scrapingbee.enabled,
            "rentcast_avm": self.rentcast.enabled,
            "rentcast_listings": self.rentcast.enabled and self.rentcast.use_as_listing_source,
            "craigslist": self.craigslist.enabled,
            "housecanary": self.housecanary.enabled,
            "county_assessor": self.county.enabled,
            "alert_discord": bool(self.alerts.discord_webhook_url),
            "alert_slack": bool(self.alerts.slack_webhook_url),
            "alert_email": self.alerts.email_enabled,
        }

    def warnings(self) -> list[str]:
        issues = []
        has_primary = (
            self.rapidapi.enabled
            or self.realtyapi.enabled
            or (self.rentcast.enabled and self.rentcast.use_as_listing_source)
            or self.craigslist.enabled
        )
        if not has_primary:
            issues.append(
                "No listing source configured. Cheapest first: CRAIGSLIST_ENABLED=true "
                "(no key at all), then RENTCAST_API_KEY (free tier), then RAPIDAPI_KEY."
            )
        if not self.scrapingbee.enabled:
            issues.append(
                "SCRAPINGBEE_API_KEY unset; no fallback when the primary APIs go stale."
            )
        if not self.alerts.any_enabled:
            issues.append("No alert channel configured; matches will only be written to SQLite.")
        if self.pipeline.enrichment.enabled and not (
            self.rentcast.enabled or self.housecanary.enabled or self.county.enabled
        ):
            issues.append("Enrichment is on but no enrichment provider is configured.")
        return issues


def _load_criteria(path: Path) -> tuple[SearchCriteria, PipelineCriteria]:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    try:
        document = yaml.safe_load(path.read_text("utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")

    raw_search = document.get("search") or {}
    raw_building = raw_search.get("building") or {}
    building = BuildingCriteria(
        min_year_built=int(raw_building.get("min_year_built", 2005)),
        allow_older_if_renovated=bool(raw_building.get("allow_older_if_renovated", True)),
        renovation_keywords=list(raw_building.get("renovation_keywords") or []),
        min_amenity_matches=int(raw_building.get("min_amenity_matches", 3)),
        luxury_amenities={
            str(k): list(v or []) for k, v in (raw_building.get("luxury_amenities") or {}).items()
        },
        required_amenities=[str(a) for a in (raw_building.get("required_amenities") or [])],
        require_amenity_evidence=bool(raw_building.get("require_amenity_evidence", False)),
    )

    search = SearchCriteria(
        cities=list(raw_search.get("cities") or []),
        state=str(raw_search.get("state", "FL")),
        zip_codes=[str(z) for z in (raw_search.get("zip_codes") or [])],
        enforce_city_whitelist=bool(raw_search.get("enforce_city_whitelist", True)),
        max_miles_from_ocean=float(raw_search.get("max_miles_from_ocean", 0.6)),
        require_coordinates=bool(raw_search.get("require_coordinates", False)),
        property_types=list(raw_search.get("property_types") or ["condo"]),
        min_beds=float(raw_search.get("min_beds", 2)),
        min_baths=float(raw_search.get("min_baths", 2)),
        min_sqft=int(raw_search.get("min_sqft", 1200)),
        require_known_sqft=bool(raw_search.get("require_known_sqft", False)),
        min_price=int(raw_search.get("min_price", 5000)),
        max_price=int(raw_search.get("max_price", 12000)),
        min_lease_months=int(raw_search.get("min_lease_months", 6)),
        max_lease_months=int(raw_search.get("max_lease_months", 12)),
        require_explicit_lease_term=bool(raw_search.get("require_explicit_lease_term", False)),
        excluded_keywords=list(raw_search.get("excluded_keywords") or []),
        positive_term_keywords=list(raw_search.get("positive_term_keywords") or []),
        building=building,
    )

    raw_pipeline = document.get("pipeline") or {}
    raw_enrichment = raw_pipeline.get("enrichment") or {}
    raw_alerts = raw_pipeline.get("alerts") or {}
    pipeline = PipelineCriteria(
        staleness_min_results=int(raw_pipeline.get("staleness_min_results", 5)),
        staleness_error_ratio=float(raw_pipeline.get("staleness_error_ratio", 0.5)),
        always_run_scraper=bool(raw_pipeline.get("always_run_scraper", False)),
        enrichment=EnrichmentCriteria(
            enabled=bool(raw_enrichment.get("enabled", True)),
            only_matched=bool(raw_enrichment.get("only_matched", True)),
            rentcast=bool(raw_enrichment.get("rentcast", True)),
            housecanary=bool(raw_enrichment.get("housecanary", False)),
            county_assessor=bool(raw_enrichment.get("county_assessor", True)),
            overpriced_threshold=float(raw_enrichment.get("overpriced_threshold", 1.15)),
            underpriced_threshold=float(raw_enrichment.get("underpriced_threshold", 0.90)),
        ),
        alerts=AlertCriteria(
            price_drop_min_dollars=int(raw_alerts.get("price_drop_min_dollars", 100)),
            price_drop_min_pct=float(raw_alerts.get("price_drop_min_pct", 0.02)),
            max_alerts_per_run=int(raw_alerts.get("max_alerts_per_run", 25)),
            cooldown_hours=int(raw_alerts.get("cooldown_hours", 24)),
            include_warned=bool(raw_alerts.get("include_warned", True)),
        ),
    )
    return search, pipeline
