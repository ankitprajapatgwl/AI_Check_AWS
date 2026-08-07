"""Factory that builds inbound webhook parser instances.

:class:`WebhookParserFactory` maps a provider key to the matching
:class:`~src.webhook_factory.webhook_master.WebhookParserMaster` subclass so
``POST /webhooks/rfq/inbound`` can decode whichever provider's payload
arrived.
Parsers are now built (and cached) per provider on demand by
:meth:`~src.services.conversation_service.ConversationService.get_parser`,
so every provider's inbound mail is understood — not just the one that used
to be pinned at startup. It mirrors
:class:`src.email_platform.factory.EmailProviderFactory` on the inbound side.

Example:
    >>> from src.webhook_factory.factory import WebhookParserFactory
    >>> from src.config import get_settings
    >>> from src.logger import AppLogger
    >>> parser = WebhookParserFactory.create(
    ...     "sendgrid", get_settings(), AppLogger.get())
    >>> parser.provider_name
    'sendgrid'
"""

import logging

from src.config import Settings
from src.email_platform.email_master import ProviderConfigError
from src.webhook_factory.alibaba_webhook import AlibabaWebhookParser
from src.webhook_factory.elasticemail_webhook import (
    ElasticEmailWebhookParser,
)
from src.webhook_factory.engagelab_webhook import EngageLabWebhookParser
from src.webhook_factory.mailgun_webhook import MailgunWebhookParser
from src.webhook_factory.sendcloud_webhook import SendCloudWebhookParser
from src.webhook_factory.sendgrid_webhook import SendGridWebhookParser
from src.webhook_factory.webhook_master import WebhookParserMaster

# Registry mapping the lowercase provider key to its parser class. Keep the
# keys identical to those in the email-provider factory so the same
# provider key can build either a send-side or receive-side instance — the
# service resolves a parser per inbound request now (see
# ``POST /webhooks/rfq/inbound``), not once at startup.
#
# NOTE: "sendcloud"'s inbound payload shape is a best-effort guess (no
# SendCloud inbound webhook doc available yet). "alibaba"/"alibaba_hk" map to
# an adapter rather than a real HTTP parser: Alibaba has no inbound webhook,
# so its mail arrives via IMAP polling and is converted from MIME instead.
_PARSERS: dict[str, type[WebhookParserMaster]] = {
    "sendgrid": SendGridWebhookParser,
    "mailgun": MailgunWebhookParser,
    "elasticemail": ElasticEmailWebhookParser,
    "sendcloud": SendCloudWebhookParser,
    "sendcloud_hk": SendCloudWebhookParser,
    "engagelab": EngageLabWebhookParser,
    "alibaba": AlibabaWebhookParser,
    "alibaba_hk": AlibabaWebhookParser,
}


class WebhookParserFactory:
    """Construct the configured inbound webhook parser instance.

    The factory is stateless; it exposes a single classmethod that performs
    the lookup-and-instantiate step.

    Example:
        >>> "engagelab" in WebhookParserFactory.supported()
        True
    """

    @classmethod
    def create(
        cls,
        provider_name: str,
        settings: Settings,
        logger: logging.Logger,
    ) -> WebhookParserMaster:
        """Instantiate the parser identified by ``provider_name``.

        Args:
            provider_name (str): Provider key, e.g. ``"mailgun"``. Matched
                case-insensitively after trimming whitespace.
            settings (Settings): Shared application configuration passed to
                the parser constructor.
            logger (logging.Logger): Shared application logger passed to the
                parser constructor.

        Returns:
            WebhookParserMaster: A fully constructed parser instance.

        Raises:
            ProviderConfigError: If ``provider_name`` is not a registered
                provider (same error type as the email-provider factory, so
                callers can handle both factories uniformly).

        Example:
            >>> parser = WebhookParserFactory.create(
            ...     "mailgun", settings, logger)   # doctest: +SKIP
            >>> parser.provider_name               # doctest: +SKIP
            'mailgun'
        """
        key = (provider_name or "").strip().lower()
        parser_cls = _PARSERS.get(key)
        if parser_cls is None:
            supported = ", ".join(_PARSERS)
            raise ProviderConfigError(
                f"Unknown email provider '{provider_name}' for webhook "
                f"parsing. Supported providers: {supported}."
            )
        logger.info("Selected inbound webhook parser: %s", key)
        return parser_cls(settings, logger)

    @classmethod
    def supported(cls) -> list[str]:
        """Return the list of registered parser keys.

        Returns:
            list[str]: Provider keys in registration order.

        Example:
            >>> "mailgun" in WebhookParserFactory.supported()
            True
        """
        return list(_PARSERS)
