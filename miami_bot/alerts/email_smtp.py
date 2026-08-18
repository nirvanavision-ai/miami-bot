"""SMTP email channel.

One digest email per run rather than one per listing: an apartment search that
fires twenty separate emails gets muted within a day. The HTML part carries the
photos; a plain-text alternative is always included for clients that refuse
HTML.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate

from ..config import AlertSettings
from ..util.logging import get_logger
from .base import AlertChannel, SendResult
from .format import AlertContent

log = get_logger(__name__)


class EmailChannel(AlertChannel):
    name = "email"

    def __init__(self, settings: AlertSettings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return self.settings.email_enabled

    def send(self, contents: list[AlertContent]) -> SendResult:
        if not contents:
            return SendResult(self.name, True, "nothing to send")

        message = EmailMessage()
        message["Subject"] = _subject(contents)
        message["From"] = formataddr(("Miami Condo Monitor", self.settings.smtp_from))
        message["To"] = ", ".join(self.settings.smtp_to)
        message["Date"] = formatdate(localtime=True)
        message.set_content(_plain_text(contents))
        message.add_alternative(_html(contents), subtype="html")

        try:
            self._deliver(message)
        except (smtplib.SMTPException, OSError) as exc:
            log.error("smtp delivery failed: %s", exc)
            return SendResult(self.name, False, str(exc))
        return SendResult(self.name, True, f"emailed {len(contents)} listing(s)")

    def _deliver(self, message: EmailMessage) -> None:
        settings = self.settings
        context = ssl.create_default_context()

        if settings.smtp_security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                settings.smtp_host, settings.smtp_port, timeout=30, context=context
            )
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)

        try:
            server.ehlo()
            if settings.smtp_security == "starttls":
                server.starttls(context=context)
                server.ehlo()
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(message)
        finally:
            try:
                server.quit()
            except smtplib.SMTPException:
                server.close()


def _subject(contents: list[AlertContent]) -> str:
    drops = sum(1 for c in contents if c.accent == "price_drop")
    new = len(contents) - drops
    if len(contents) == 1:
        return f"{contents[0].emoji} {contents[0].headline}"
    parts = []
    if new:
        parts.append(f"{new} new")
    if drops:
        parts.append(f"{drops} price drop{'s' if drops != 1 else ''}")
    return f"Miami condos: {', '.join(parts)}"


def _plain_text(contents: list[AlertContent]) -> str:
    lines: list[str] = [f"{len(contents)} listing update(s)", ""]
    for content in contents:
        lines.append(f"{content.emoji} {content.headline}")
        lines.append(f"   {content.subheadline}")
        for name, value in content.fields:
            lines.append(f"   {name}: {value}")
        if content.url:
            lines.append(f"   {content.url}")
        lines.append(f"   {content.footer}")
        lines.append("")
    return "\n".join(lines)


def _html(contents: list[AlertContent]) -> str:
    cards: list[str] = []
    for content in contents:
        rows = "".join(
            f"<tr><td style='padding:3px 12px 3px 0;color:#5b6672;white-space:nowrap;"
            f"font-size:13px'>{_esc(name)}</td>"
            f"<td style='padding:3px 0;color:#12202e;font-size:13px'>{_esc(value)}</td></tr>"
            for name, value in content.fields
        )
        photo = (
            f"<img src='{_esc(content.photo)}' alt='' width='100%' "
            "style='display:block;border-radius:8px 8px 0 0;max-height:280px;object-fit:cover'>"
            if content.photo else ""
        )
        title = _esc(content.headline)
        if content.url:
            title = (
                f"<a href='{_esc(content.url)}' "
                f"style='color:#0b6e63;text-decoration:none'>{title}</a>"
            )
        accent = "#0FB9A5" if content.accent == "new" else "#F2A33C"
        cards.append(
            f"""
    <div style="border:1px solid #e2e8ee;border-radius:10px;overflow:hidden;margin:0 0 20px 0">
      {photo}
      <div style="padding:14px 16px;border-top:3px solid {accent}">
        <h2 style="margin:0 0 4px;font-size:17px;line-height:1.3">{content.emoji} {title}</h2>
        <p style="margin:0 0 10px;color:#31414f;font-size:14px"><strong>{_esc(content.subheadline)}</strong></p>
        <p style="margin:0 0 12px;color:#5b6672;font-size:13px;line-height:1.5">{_esc(content.description)}</p>
        <table style="border-collapse:collapse">{rows}</table>
        <p style="margin:12px 0 0;color:#8a97a4;font-size:11px">{_esc(content.footer)}</p>
      </div>
    </div>"""
        )

    return f"""<!doctype html>
<html><body style="margin:0;padding:20px;background:#f5f7f9;
                   font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif">
  <div style="max-width:640px;margin:0 auto">
    <h1 style="font-size:15px;color:#5b6672;font-weight:600;margin:0 0 18px">
      Miami coastal condo monitor — {len(contents)} update(s)
    </h1>
    {''.join(cards)}
    <p style="color:#98a4b0;font-size:11px;margin-top:24px">
      Automated search: Miami Beach · Surfside · Bal Harbour · Sunny Isles Beach.
      Lease terms and square footage are parsed from listing copy and county records;
      confirm both with the listing agent before signing.
    </p>
  </div>
</body></html>"""


def _esc(text: str | None) -> str:
    if not text:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )
