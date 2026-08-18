"""Discord webhook channel.

Discord caps a message at 10 embeds and 6000 characters across them, so batches
are chunked. The primary photo goes in ``image`` rather than ``thumbnail`` --
for an apartment hunt the photo is the first thing worth seeing.
"""

from __future__ import annotations

from ..util.http import HttpClient, HttpError
from ..util.logging import get_logger
from .base import AlertChannel, SendResult
from .format import AlertContent

log = get_logger(__name__)

MAX_EMBEDS_PER_MESSAGE = 10
MAX_FIELD_VALUE = 1024


class DiscordChannel(AlertChannel):
    name = "discord"

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
        for start in range(0, len(contents), MAX_EMBEDS_PER_MESSAGE):
            batch = contents[start : start + MAX_EMBEDS_PER_MESSAGE]
            payload = {
                "username": "Miami Condo Monitor",
                "content": _summary_line(batch, start == 0, len(contents)),
                "embeds": [self._embed(content) for content in batch],
                "allowed_mentions": {"parse": []},
            }
            try:
                self.http.request(
                    "POST", self.webhook_url, json_body=payload, allow_cache=False
                )
                sent += len(batch)
            except HttpError as exc:
                log.error("discord webhook failed: %s", exc)
                return SendResult(self.name, False, f"sent {sent}/{len(contents)}: {exc}")
        return SendResult(self.name, True, f"sent {sent} embed(s)")

    def _embed(self, content: AlertContent) -> dict[str, object]:
        embed: dict[str, object] = {
            "title": f"{content.emoji} {content.headline}"[:256],
            "description": f"**{content.subheadline}**\n{content.description}".strip()[:4096],
            "color": content.color,
            "fields": [
                {"name": name[:256], "value": (value or "—")[:MAX_FIELD_VALUE], "inline": True}
                for name, value in content.fields[:24]
            ],
            "footer": {"text": content.footer[:2048]},
        }
        if content.url:
            embed["url"] = content.url
        if content.photo:
            embed["image"] = {"url": content.photo}
        return embed


def _summary_line(batch: list[AlertContent], is_first: bool, total: int) -> str:
    if not is_first:
        return ""
    drops = sum(1 for c in batch if c.accent == "price_drop")
    new = total - drops
    parts = []
    if new:
        parts.append(f"**{new}** new match{'es' if new != 1 else ''}")
    if drops:
        parts.append(f"**{drops}** price drop{'s' if drops != 1 else ''}")
    return " · ".join(parts) if parts else f"**{total}** update(s)"
