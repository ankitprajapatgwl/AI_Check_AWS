"""Outbound email provider backed by the Elastic Email REST API.

:class:`ElasticEmailProvider` implements :meth:`EmailMaster.send_email`
using the official :mod:`ElasticEmail` Python SDK (the
``Emails.Transactional`` endpoint). The ``From`` header uses the verified
sender, and the RFC ``Message-ID`` this app mints is passed through
``EmailContent(headers=…)``. No ``Reply-To`` is set — replies come back to
the ``From`` address (routed to this app's webhook by Elastic Email's inbound
route) and are threaded by ``Message-ID``/``References`` plus the
``[RFQ - id]`` subject prefix.

Configuration consumed (see :class:`src.config.Settings`):

- ``ELASTICEMAIL_API_KEY`` *(required)* – API key with **Send** access.
- ``ELASTICEMAIL_OUTBOUND_DOMAIN`` *(required)* – domain used to build the
  ``From`` address for sends made through Elastic Email.
- ``ELASTICEMAIL_COMPANY_NAME`` – display name in the ``From`` header
  (defaults to ``"Your Company"``).

Example:
    >>> from src.email_platform.elasticemail_provider import (
    ...     ElasticEmailProvider)
    >>> from src.config import get_settings
    >>> from src.logger import AppLogger
    >>> provider = ElasticEmailProvider(get_settings(), AppLogger.get())
    >>> provider.provider_name                       # doctest: +SKIP
    'elasticemail'
"""

import logging

import ElasticEmail
from ElasticEmail.api.emails_api import EmailsApi
from ElasticEmail.models.body_content_type import BodyContentType
from ElasticEmail.models.body_part import BodyPart
from ElasticEmail.models.email_content import EmailContent
from ElasticEmail.models.email_transactional_message_data import (
    EmailTransactionalMessageData,
)
from ElasticEmail.models.transactional_recipient import (
    TransactionalRecipient,
)

from src.config import Settings
from src.email_platform.email_master import EmailMaster, EmailSendError

# Elastic Email's SDK looks up the API key in ``Configuration.api_key``
# under this scheme name.
_API_KEY_SCHEME = "apikey"


class ElasticEmailProvider(EmailMaster):
    """Send RFQ emails through the Elastic Email transactional API.

    Inherits all address and template helpers from :class:`EmailMaster` and
    adds Elastic-Email-specific transmission via its official SDK.

    Attributes:
        settings (Settings): Shared application configuration.
        log (logging.Logger): Shared application logger.

    Example:
        >>> provider = ElasticEmailProvider(settings, logger)
        >>> result = provider.send_email(            # doctest: +SKIP
        ...     from_email="noreply@yourdomain.com", from_name="Acme",
        ...     to_email="buyer@x.com", to_name="Buyer",
        ...     subject="[RFQ - hd273hsd] - Hi", html_body="<p>Hi</p>",
        ...     message_id="<rfq.hd273hsd.ab@mail.yourdomain.com>")
        >>> result["provider"]                       # doctest: +SKIP
        'elasticemail'
    """

    def __init__(
        self, settings: Settings, logger: logging.Logger
    ) -> None:
        """Validate the API key and build a reusable SDK configuration.

        Args:
            settings (Settings): Shared application configuration.
            logger (logging.Logger): Shared application logger.

        Returns:
            None

        Raises:
            ProviderConfigError: If ``ELASTICEMAIL_API_KEY`` is not set.
        """
        super().__init__(settings, logger)
        api_key = self._require(
            settings.elasticemail_api_key, "ELASTICEMAIL_API_KEY"
        )
        self._configuration = ElasticEmail.Configuration(
            host=settings.elasticemail_api_url
        )
        self._configuration.api_key[_API_KEY_SCHEME] = api_key
        self.log.info("Elastic Email provider initialised")

    @property
    def provider_name(self) -> str:
        """Return the provider key ``"elasticemail"``.

        Returns:
            str: Always ``"elasticemail"``.
        """
        return "elasticemail"

    def send_email(
        self,
        *,
        from_email: str,
        from_name: str,
        to_email: str,
        to_name: str,
        subject: str,
        html_body: str,
        message_id: str,
        extra_headers: dict[str, str] | None = None,
        attachments: list | None = None,
    ) -> dict:
        """Send one email via Elastic Email's transactional endpoint.

        Builds an :class:`EmailTransactionalMessageData` payload with a
        single HTML body part and the verified ``From`` header, then submits
        it through the SDK. The response carries the Elastic Email
        transaction / message id. No ``Reply-To`` is set — replies thread on
        ``Message-ID``/``References`` and the ``[RFQ - id]`` subject prefix.

        Args:
            from_email (str): Verified sender address.
            from_name (str): Sender display name.
            to_email (str): Recipient address.
            to_name (str): Recipient display name (unused by the API but
                accepted for interface parity).
            subject (str): Subject line.
            html_body (str): HTML body.
            message_id (str): The RFC ``Message-ID`` this app minted, sent
                through ``EmailContent(headers=…)``.
            extra_headers (dict[str, str] | None): Extra headers merged into
                the same dict, e.g. ``X-RFQ-Conversation-Id``.
            attachments (list | None): Accepted for interface parity; the
                transactional payload built here carries no attachment part.

        Returns:
            dict: ``{"status_code": int, "provider": "elasticemail",
                "provider_message_id": str | None, "message_id": str}``.
                ``status_code`` is ``202`` on success to mirror the other
                providers' "queued" semantics.

        Raises:
            EmailSendError: If the SDK raises or the API rejects the send.

        Example:
            >>> provider.send_email(                  # doctest: +SKIP
            ...     from_email="noreply@yourdomain.com",
            ...     from_name="Acme", to_email="buyer@x.com",
            ...     to_name="Buyer", subject="[RFQ - hd273hsd] - Hi",
            ...     html_body="<p>Hi</p>",
            ...     message_id="<rfq.hd273hsd.ab@mail.yourdomain.com>")
            {'status_code': 202, 'provider': 'elasticemail', ...}
        """
        # Elastic Email expects the sender as a single "Name <addr>" string.
        from_header = f"{from_name} <{from_email}>"
        content = EmailContent(
            body=[
                BodyPart(
                    content_type=BodyContentType.HTML,
                    content=html_body,
                    charset="utf-8",
                )
            ],
            var_from=from_header,
            headers={"Message-ID": message_id, **(extra_headers or {})},
            subject=subject,
        )
        message = EmailTransactionalMessageData(
            recipients=TransactionalRecipient(to=[to_email]),
            content=content,
        )

        try:
            with ElasticEmail.ApiClient(self._configuration) as client:
                api = EmailsApi(client)
                result = api.emails_transactional_post(message)
        except Exception as exc:  # noqa: BLE001 - normalise to one type
            self.log.error("Elastic Email send failed: %s", exc)
            raise EmailSendError(
                f"Elastic Email failed to send to {to_email}: {exc}"
            ) from exc

        # The SDK returns an ``EmailSend`` object exposing a transaction id
        # and/or message id; surface whichever is present. This is Elastic
        # Email's own id, distinct from the RFC Message-ID we minted above.
        provider_message_id = (
            getattr(result, "message_id", None)
            or getattr(result, "transaction_id", None)
        )
        self.log.info(
            "Elastic Email accepted email to %s (txn=%s)",
            to_email,
            provider_message_id,
        )
        return {
            "status_code": 202,
            "provider": self.provider_name,
            "provider_message_id": (
                str(provider_message_id) if provider_message_id else None
            ),
            "message_id": message_id,
        }
