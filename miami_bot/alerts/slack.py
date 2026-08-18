"""Slack incoming-webhook channel.

Slack's Block Kit caps a message at 50 blocks, and each listing costs roughly
three (header, fields, divider), so batches are chunked conservatively.
"""

from __future__ import annotations

from ..util.http import HttpClient, HttpError
from ..util.logging import get_logger
from .base import AlertChannel, SendResult
from .format import AlertContent

log = get_logger(__name__)

MAX_LISTINGS_PER_MESSAGE = 12
MAX_FIELDS_PER_SECTION = 10          # Slack hard limit


class SlackChannel(AlertChannel):
    name = "slack"

    def __init__(self, http: HttpClient, webhook_url: str) -> None:
        self.http = http
        self.webhook_url = webhook_url

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def send(self, contents: list[AlertContent]) -> SendResult:
        if not contents:
            return SendResult(self.name, True, "nothing to send")

        sent = 0
        for start in range(0, len(contents), MAX_LISTINGS_PER_MESSAGE):
            batch = contents[start : start + MAX_LISTINGS_PER_MESSAGE]
            blocks: list[dict[str, object]] = [{
                "type": "header",
                "text": {"type": "plain_text",
                         "text": f"Miami condo monitor — {len(contents)} update(s)"[:150]},
            }]
            for content in batch:
                blocks.extend(self._blocks(content))

            payload = {
                "text": f"{len(contents)} listing update(s)",   # notification fallback
                "blocks": blocks[:50],
            }
            try:
                self.http.request(
                    "POST", self.webhook_url, json_body=payload, allow_cache=False
                )
                sent += len(batch)
            except HttpError as exc:
                log.error("slack webhook failed: %s", exc)
                return SendResult(self.name, False, f"sent {sent}/{len(contents)}: {exc}")
        return SendResult(self.name, True, f"sent {sent} listing(s)")

    def _blocks(self, content: AlertContent) -> list[dict[str, object]]:
        title = f"{content.emoji} *{_escape(content.headline)}*"
        if content.url:
            title = f"{content.emoji} *<{content.url}|{_escape(content.headline)}>*"

        section: dict[str, object] = {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{title}\n{_escape(content.subheadline)}"},
        }
        if content.photo:
            section["accessory"] = {
                "type": "image",
                "image_url": content.photo,
                "alt_text": content.headline[:150],
            }

        blocks: list[dict[str, object]] = [section]
        fields = [
            {"type": "mrkdwn", "text": f"*{_escape(name)}*\n{_escape(value or '—')}"[:2000]}
            for name, value in content.fields[:MAX_FIELDS_PER_SECTION]
        ]
        if fields:
            blocks.append({"type": "section", "fields": fields})
        if content.footer:
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": _escape(content.footer)}],
            })
        blocks.append({"type": "divider"})
        return blocks


def _escape(text: str) -> str:
    """Slack mrkdwn requires these three entities to be escaped."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
