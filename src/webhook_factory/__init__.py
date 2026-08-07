"""Pluggable inbound webhook parsers for EmailPOC.

This package implements the *strategy + factory* pattern for the single
inbound webhook. Every provider posts received mail in a different shape;
each parser normalises that into one :class:`InboundEmail` so the
``POST /webhooks/rfq/inbound`` route and the service layer stay
provider-agnostic.

- :mod:`src.webhook_factory.webhook_master` defines
  :class:`WebhookParserMaster` (the abstract base, attachment persistence
  and signature checking) plus the :class:`InboundEmail` /
  :class:`RawAttachment` data models.
- :mod:`src.webhook_factory.sendgrid_webhook`,
  :mod:`src.webhook_factory.mailgun_webhook` and
  :mod:`src.webhook_factory.elasticemail_webhook` are the concrete parsers.
- :mod:`src.webhook_factory.factory` exposes
  :class:`WebhookParserFactory`, which returns the parser for a given
  provider key. One is built eagerly at startup
  (``src.app._INBOUND_EMAIL_PROVIDER``) to validate its credentials; the
  rest are built on demand, because ``POST /webhooks/rfq/inbound`` resolves
  the posting provider per request (see
  :meth:`~src.services.conversation_service.ConversationService.get_parser`).

Import the factory (not the concrete parsers):

    >>> from src.webhook_factory.factory import WebhookParserFactory
"""

from src.webhook_factory.factory import WebhookParserFactory
from src.webhook_factory.webhook_master import (
    InboundEmail,
    RawAttachment,
    WebhookParseError,
    WebhookParserMaster,
)

__all__ = [
    "WebhookParserFactory",
    "WebhookParserMaster",
    "InboundEmail",
    "RawAttachment",
    "WebhookParseError",
]
