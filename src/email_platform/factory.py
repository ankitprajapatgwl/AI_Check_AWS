"""Factory that builds outbound email provider instances.

:class:`EmailProviderFactory` maps a provider key — picked per send on the
Send RFQ form's "Provider" dropdown, see
:meth:`~src.services.conversation_service.ConversationService.get_provider`
— to the matching :class:`~src.email_platform.email_master.EmailMaster`
subclass and returns a ready-to-use instance. Callers never import a
concrete provider directly, so adding a provider is a new subclass + one
line here, with no changes anywhere else.

Example:
    >>> from src.email_platform.factory import EmailProviderFactory
    >>> from src.config import get_settings
    >>> from src.logger import AppLogger
    >>> provider = EmailProviderFactory.create(
    ...     "sendgrid", get_settings(), AppLogger.get())
    >>> provider.provider_name
    'sendgrid'
"""

import logging

from src.config import Settings
from src.email_platform.alibaba_provider import (
    AlibabaEnterpriseProvider,
    AlibabaHKEnterpriseProvider,
)
from src.email_platform.elasticemail_provider import ElasticEmailProvider
from src.email_platform.email_master import EmailMaster, ProviderConfigError
from src.email_platform.engagelab_provider import EngageLabEmailProvider
from src.email_platform.mailgun_provider import MailgunEmailProvider
from src.email_platform.sendcloud_provider import (
    SendCloudEmailProvider,
    SendCloudHKEmailProvider,
)
from src.email_platform.sendgrid_provider import SendGridEmailProvider

# Registry mapping the lowercase provider key to its implementation class.
# Add a new provider by writing its class and registering it here — nothing
# else in the application needs to change.
#
# "sendcloud_hk" and "alibaba_hk" are not user-facing "Provider" choices on
# the Send RFQ form — they're the internal keys src.route resolves to when
# the sender picks SendCloud/Alibaba for a Chinese supplier (see
# src.route._SEND_KEYS), pinning the send to that provider's Hong Kong region
# instead of its Singapore default.
_PROVIDERS: dict[str, type[EmailMaster]] = {
    "sendgrid": SendGridEmailProvider,
    "mailgun": MailgunEmailProvider,
    "elasticemail": ElasticEmailProvider,
    "sendcloud": SendCloudEmailProvider,
    "sendcloud_hk": SendCloudHKEmailProvider,
    "engagelab": EngageLabEmailProvider,
    "alibaba": AlibabaEnterpriseProvider,
    "alibaba_hk": AlibabaHKEnterpriseProvider,
}


class EmailProviderFactory:
    """Construct the configured email provider instance.

    The factory is stateless; it exposes a single classmethod that performs
    the lookup-and-instantiate step.

    Example:
        >>> "alibaba_hk" in EmailProviderFactory.supported()
        True
    """

    @classmethod
    def create(
        cls,
        provider_name: str,
        settings: Settings,
        logger: logging.Logger,
    ) -> EmailMaster:
        """Instantiate the provider identified by ``provider_name``.

        Args:
            provider_name (str): Provider key, e.g. ``"sendgrid"``. Matched
                case-insensitively after trimming whitespace.
            settings (Settings): Shared application configuration passed to
                the provider constructor.
            logger (logging.Logger): Shared application logger passed to the
                provider constructor.

        Returns:
            EmailMaster: A fully constructed provider instance.

        Raises:
            ProviderConfigError: If ``provider_name`` is not a registered
                provider. The message lists the supported keys.

        Example:
            >>> provider = EmailProviderFactory.create(
            ...     "mailgun", settings, logger)   # doctest: +SKIP
            >>> provider.provider_name             # doctest: +SKIP
            'mailgun'
        """
        key = (provider_name or "").strip().lower()
        provider_cls = _PROVIDERS.get(key)
        if provider_cls is None:
            supported = ", ".join(_PROVIDERS)
            raise ProviderConfigError(
                f"Unknown email provider '{provider_name}'. "
                f"Supported providers: {supported}."
            )
        logger.info("Selected email provider: %s", key)
        return provider_cls(settings, logger)

    @classmethod
    def supported(cls) -> list[str]:
        """Return the list of registered provider keys.

        Returns:
            list[str]: Provider keys in registration order.

        Example:
            >>> "sendgrid" in EmailProviderFactory.supported()
            True
        """
        return list(_PROVIDERS)
