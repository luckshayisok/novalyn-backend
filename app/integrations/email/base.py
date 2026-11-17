"""Email sending interface. Providers implement EmailSender."""

from dataclasses import asdict, dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    text: str
    html: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EmailMessage":
        return cls(to=data["to"], subject=data["subject"], text=data["text"], html=data["html"])


class EmailSender(Protocol):
    def send(self, message: EmailMessage) -> None:
        """Deliver the message or raise."""
