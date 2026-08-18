"""Alert channel contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from .format import AlertContent


@dataclass
class SendResult:
    channel: str
    ok: bool
    detail: str = ""


class AlertChannel(ABC):
    name: str = "channel"

    @property
    @abstractmethod
    def enabled(self) -> bool:
        """False when the channel is not configured."""

    @abstractmethod
    def send(self, contents: list[AlertContent]) -> SendResult:
        """Deliver a batch of alerts. Must not raise."""
