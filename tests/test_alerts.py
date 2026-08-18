"""Alert gating, formatting and delivery."""

from __future__ import annotations

import json

import pytest
import responses

from miami_bot.alerts.discord import DiscordChannel
from miami_bot.alerts.dispatcher import AlertDispatcher, AlertEvent
from miami_bot.alerts.email_smtp import EmailChannel, _html, _subject
from miami_bot.alerts.format import build_content
from miami_bot.alerts.slack import SlackChannel
from miami_bot.filters import evaluate
from miami_bot.models import MatchResult
from tests.conftest import make_listing


@pytest.fixture
def enriched_listing():
    listing = make_listing(broker="Douglas Elliman", days_on_market=6)
    listing.enrichment.rent_estimate = 8900
    listing.enrichment.rent_estimate_low = 8300
    listing.enrichment.rent_estimate_high = 9500
    listing.enrichment.rent_estimate_source = "rentcast"
    listing.enrichment.verdict = "at market"
    listing.enrichment.county_sqft = 1450
    return listing


# ------------------------------------------------------------------ content
def test_content_carries_everything_the_brief_asks_for(enriched_listing, criteria):
    result = evaluate(enriched_listing, criteria)
    content = build_content(enriched_listing, warnings=result.warnings, score=result.score)
    labels = {name for name, _ in content.fields}

    assert content.photo == "https://example.com/photo.jpg"      # primary photo
    assert content.url == "https://example.com/listing/1"        # source URL
    assert "Price" in labels                                     # price
    assert "Layout" in labels                                    # specs
    assert "Lease term" in labels                                # parsed lease terms
    assert dict(content.fields)["Lease term"] == "6-12 months"
    assert any(label.startswith("Amenities") for label in labels)
    assert any(label.startswith("Rent AVM") for label in labels)


def test_a_price_drop_shows_the_delta(enriched_listing):
    content = build_content(enriched_listing, kind="price_drop", old_price=10500)
    assert content.headline.startswith("Price drop")
    assert "$10,500 → $9,500" in content.subheadline
    assert "−$1,000" in content.subheadline and "9.5%" in content.subheadline
    assert content.accent == "price_drop"


def test_multi_portal_listings_say_where_else_they_appear(enriched_listing):
    content = build_content(enriched_listing, extra_sources=["rapidapi_redfin"])
    assert dict(content.fields)["Also listed on"] == "rapidapi_redfin"


def test_a_county_conflict_is_surfaced(enriched_listing):
    enriched_listing.enrichment.county_conflict = True
    content = build_content(enriched_listing)
    assert "conflicts" in dict(content.fields)["County record"]


# ------------------------------------------------------------------ channels
@responses.activate
def test_discord_payload_shape(http, enriched_listing):
    responses.add(responses.POST, "https://discord.test/webhook", status=204)
    content = build_content(enriched_listing)
    result = DiscordChannel(http, "https://discord.test/webhook").send([content])
    assert result.ok

    body = json.loads(responses.calls[0].request.body)
    embed = body["embeds"][0]
    assert embed["url"] == enriched_listing.url
    assert embed["image"]["url"] == enriched_listing.primary_photo
    assert len(embed["fields"]) <= 25
    assert len(embed["title"]) <= 256


@responses.activate
def test_discord_chunks_large_batches(http, enriched_listing):
    responses.add(responses.POST, "https://discord.test/webhook", status=204)
    contents = [build_content(enriched_listing) for _ in range(23)]
    result = DiscordChannel(http, "https://discord.test/webhook").send(contents)
    assert result.ok and len(responses.calls) == 3      # 10 + 10 + 3


@responses.activate
def test_a_webhook_failure_is_reported_not_raised(http, enriched_listing):
    responses.add(responses.POST, "https://discord.test/webhook", status=500)
    result = DiscordChannel(http, "https://discord.test/webhook").send(
        [build_content(enriched_listing)]
    )
    assert not result.ok and "500" in result.detail


@responses.activate
def test_slack_block_kit_limits(http, enriched_listing):
    responses.add(responses.POST, "https://slack.test/webhook", status=200)
    content = build_content(enriched_listing)
    assert SlackChannel(http, "https://slack.test/webhook").send([content]).ok

    body = json.loads(responses.calls[0].request.body)
    assert body["text"]                                  # notification fallback
    assert len(body["blocks"]) <= 50
    fields_block = next(b for b in body["blocks"] if b.get("fields"))
    assert len(fields_block["fields"]) <= 10             # Slack's hard limit
    assert body["blocks"][1]["accessory"]["type"] == "image"


def test_slack_escapes_markup(http):
    listing = make_listing(broker="Smith & Jones <Realty>")
    content = build_content(listing)
    blocks = SlackChannel(http, "https://slack.test/webhook")._blocks(content)
    rendered = json.dumps(blocks)
    assert "&amp;" in rendered and "&lt;Realty&gt;" in rendered


def test_listing_copy_is_stripped_of_markup_when_parsed():
    """First defence: clean_text removes tags from provider-supplied copy."""
    listing = make_listing(description="Contact <b>agent</b> <script>alert(1)</script> & book")
    content = build_content(listing)
    assert "<b>" not in content.description and "<script>" not in content.description
    assert "agent" in content.description


def test_email_html_escapes_fields_that_reach_the_template_verbatim():
    """Second defence: anything rendered into HTML is entity-escaped."""
    listing = make_listing(broker="<script>alert(1)</script> & Co")
    html = _html([build_content(listing)])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html and "&amp; Co" in html


def test_email_subject_counts_both_kinds():
    listing = make_listing()
    contents = [
        build_content(listing),
        build_content(listing, kind="price_drop", old_price=10500),
    ]
    assert _subject(contents) == "Miami condos: 1 new, 1 price drop"


def test_channels_are_disabled_without_configuration(http, settings):
    assert not DiscordChannel(http, "").enabled
    assert not SlackChannel(http, "").enabled
    assert not EmailChannel(settings.alerts).enabled


# ---------------------------------------------------------------- dispatcher
def _dispatcher(http, settings, db, **overrides):
    settings.alerts.discord_webhook_url = overrides.pop(
        "discord", "https://discord.test/webhook"
    )
    for key, value in overrides.items():
        setattr(settings.pipeline.alerts, key, value)
    return AlertDispatcher(http, settings, db)


def _match(listing, criteria) -> MatchResult:
    return evaluate(listing, criteria)


def test_a_new_match_produces_an_alert(http, settings, db, criteria, listing):
    dispatcher = _dispatcher(http, settings, db)
    change = db.upsert(listing, matched=True)
    event = dispatcher.build_event(_match(listing, criteria), change)
    assert event is not None and event.kind == "new"


def test_a_failing_listing_never_alerts(http, settings, db, criteria):
    dispatcher = _dispatcher(http, settings, db)
    listing = make_listing(price=25000)
    change = db.upsert(listing)
    assert dispatcher.build_event(_match(listing, criteria), change) is None


def test_an_unchanged_listing_does_not_re_alert(http, settings, db, criteria, listing):
    dispatcher = _dispatcher(http, settings, db)
    db.upsert(listing, matched=True)
    change = db.upsert(listing, matched=True)
    assert dispatcher.build_event(_match(listing, criteria), change) is None


def test_a_material_price_drop_alerts(http, settings, db, criteria):
    dispatcher = _dispatcher(http, settings, db)
    db.upsert(make_listing(price=9500), matched=True)
    dropped = make_listing(price=8800)
    change = db.upsert(dropped, matched=True)
    event = dispatcher.build_event(_match(dropped, criteria), change)
    assert event is not None and event.kind == "price_drop" and event.old_price == 9500


def test_a_trivial_price_drop_stays_quiet(http, settings, db, criteria):
    dispatcher = _dispatcher(http, settings, db)
    db.upsert(make_listing(price=9500), matched=True)
    nudged = make_listing(price=9450)          # -$50, -0.5%: below both floors
    change = db.upsert(nudged, matched=True)
    assert dispatcher.build_event(_match(nudged, criteria), change) is None


def test_a_price_increase_stays_quiet(http, settings, db, criteria):
    dispatcher = _dispatcher(http, settings, db)
    db.upsert(make_listing(price=9500), matched=True)
    raised = make_listing(price=10200)
    change = db.upsert(raised, matched=True)
    assert dispatcher.build_event(_match(raised, criteria), change) is None


def test_the_cooldown_suppresses_a_repeat(http, settings, db, criteria, listing):
    dispatcher = _dispatcher(http, settings, db)
    change = db.upsert(listing, matched=True)
    db.record_alert(change.dedupe_key, "new", "discord", listing.price, True)
    assert dispatcher.build_event(_match(listing, criteria), change) is None


@responses.activate
def test_dispatch_caps_a_run_and_keeps_the_best(http, settings, db, criteria):
    responses.add(responses.POST, "https://discord.test/webhook", status=204)
    dispatcher = _dispatcher(http, settings, db, max_alerts_per_run=2)
    events = [
        AlertEvent(listing=make_listing(), kind="new", dedupe_key=f"k{i}", score=float(i))
        for i in range(5)
    ]
    dispatcher.dispatch(events)
    body = json.loads(responses.calls[0].request.body)
    assert len(body["embeds"]) == 2


@responses.activate
def test_price_drops_are_dispatched_ahead_of_new_matches(http, settings, db):
    responses.add(responses.POST, "https://discord.test/webhook", status=204)
    dispatcher = _dispatcher(http, settings, db, max_alerts_per_run=1)
    events = [
        AlertEvent(listing=make_listing(), kind="new", dedupe_key="a", score=99.0),
        AlertEvent(listing=make_listing(), kind="price_drop", dedupe_key="b",
                   old_price=10500, score=1.0),
    ]
    dispatcher.dispatch(events)
    body = json.loads(responses.calls[0].request.body)
    assert body["embeds"][0]["title"].startswith("📉")


@responses.activate
def test_dry_run_sends_nothing(http, settings, db):
    responses.add(responses.POST, "https://discord.test/webhook", status=204)
    settings.dry_run = True
    dispatcher = _dispatcher(http, settings, db)
    dispatcher.dispatch([AlertEvent(listing=make_listing(), kind="new", dedupe_key="a")])
    assert len(responses.calls) == 0


@responses.activate
def test_delivery_is_recorded_for_the_cooldown(http, settings, db, listing):
    responses.add(responses.POST, "https://discord.test/webhook", status=204)
    dispatcher = _dispatcher(http, settings, db)
    change = db.upsert(listing, matched=True)
    dispatcher.dispatch([AlertEvent(listing=listing, kind="new",
                                    dedupe_key=change.dedupe_key)])
    assert db.was_alerted_recently(change.dedupe_key, "new", 24)


# ------------------------------------------------------------- SMTP delivery
def _email_settings(settings, security="starttls"):
    settings.alerts.smtp_host = "smtp.example.com"
    settings.alerts.smtp_port = 587
    settings.alerts.smtp_username = "bot@example.com"
    settings.alerts.smtp_password = "secret"
    settings.alerts.smtp_from = "bot@example.com"
    settings.alerts.smtp_to = ["me@example.com"]
    settings.alerts.smtp_security = security
    return settings


def test_smtp_starttls_flow(monkeypatch, settings, listing):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            calls.append(("connect", host, port))

        def ehlo(self):
            calls.append(("ehlo",))

        def starttls(self, context=None):
            calls.append(("starttls",))

        def login(self, user, password):
            calls.append(("login", user))

        def send_message(self, message):
            calls.append(("send", message["To"], message["Subject"]))

        def quit(self):
            calls.append(("quit",))

    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    result = EmailChannel(_email_settings(settings).alerts).send([build_content(listing)])

    assert result.ok
    actions = [c[0] for c in calls]
    assert actions == ["connect", "ehlo", "starttls", "ehlo", "login", "send", "quit"]
    assert ("send", "me@example.com", f"🏝️ {listing.display_address}") in calls


def test_smtp_ssl_flow_skips_starttls(monkeypatch, settings, listing):
    calls = []

    class FakeSMTPSSL:
        def __init__(self, host, port, timeout=None, context=None):
            calls.append("connect")

        def ehlo(self):
            calls.append("ehlo")

        def login(self, user, password):
            calls.append("login")

        def send_message(self, message):
            calls.append("send")

        def quit(self):
            calls.append("quit")

    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSMTPSSL)
    result = EmailChannel(_email_settings(settings, "ssl").alerts).send([build_content(listing)])
    assert result.ok and "starttls" not in calls


def test_an_smtp_failure_is_reported_not_raised(monkeypatch, settings, listing):
    import smtplib

    class FailingSMTP:
        def __init__(self, *args, **kwargs):
            raise smtplib.SMTPConnectError(421, "service not available")

    monkeypatch.setattr("smtplib.SMTP", FailingSMTP)
    result = EmailChannel(_email_settings(settings).alerts).send([build_content(listing)])
    assert not result.ok and "421" in result.detail


def test_the_email_carries_both_a_plain_and_an_html_part(monkeypatch, settings, listing):
    captured = {}

    class CapturingSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def ehlo(self):
            pass

        def starttls(self, context=None):
            pass

        def login(self, *args):
            pass

        def send_message(self, message):
            captured["message"] = message

        def quit(self):
            pass

    monkeypatch.setattr("smtplib.SMTP", CapturingSMTP)
    EmailChannel(_email_settings(settings).alerts).send([build_content(listing)])

    message = captured["message"]
    subtypes = {part.get_content_subtype() for part in message.walk()}
    assert "plain" in subtypes and "html" in subtypes
    html = message.get_body(("html",)).get_content()
    assert listing.primary_photo in html
    assert listing.url in html
