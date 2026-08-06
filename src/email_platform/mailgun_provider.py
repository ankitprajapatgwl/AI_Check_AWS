"""Outbound email provider backed by the Mailgun HTTP API.

:class:`MailgunEmailProvider` implements :meth:`EmailMaster.send_email` by
POSTing to Mailgun's ``/v3/{domain}/messages`` endpoint with the
:mod:`requests` library — the approach shown throughout Mailgun's official
Python documentation (Mailgun does not ship a first-party Python SDK). The
``From`` header uses the verified sender; the RFC ``Message-ID`` this app
mints rides along as the ``h:Message-Id`` form field (``h:``-prefixed fields
are passed through as raw headers). No ``Reply-To`` is sent — replies thread
on ``Message-ID``/``References`` and the ``[RFQ - id]`` subject prefix.

Configuration consumed (see :class:`src.config.Settings`):

- ``MAILGUN_API_KEY`` *(required)* – Mailgun private API key.
- ``MAILGUN_DOMAIN`` *(required)* – verified sending domain used in the
  Mailgun API endpoint path (``/v3/{domain}/messages``).
- ``MAILGUN_API_BASE`` – ``https://api.mailgun.net`` (US, default) or
  ``https://api.eu.mailgun.net`` (EU).
- ``MAILGUN_OUTBOUND_DOMAIN`` *(required)* – domain used to build the
  ``From`` address (keep it equal to ``MAILGUN_DOMAIN`` above unless you
  know Mailgun accepts a mismatch).
- ``MAILGUN_COMPANY_NAME`` – display name in the ``From`` header (defaults
  to ``"Your Company"``).

Example:
    >>> from src.email_platform.mailgun_provider import (
    ...     MailgunEmailProvider)
    >>> from src.config import get_settings
    >>> from src.logger import AppLogger
    >>> provider = MailgunEmailProvider(get_settings(), AppLogger.get())
    >>> provider.provider_name                       # doctest: +SKIP
    'mailgun'
"""

import logging

import requests

from src.config import Settings
from src.email_platform.email_master import EmailMaster, EmailSendError

# Mailgun authenticates HTTP API calls with HTTP Basic auth where the
# username is the literal string "api" and the password is the API key.
_BASIC_AUTH_USER = "api"

# Network timeout (connect, read) in seconds for the send request.
_REQUEST_TIMEOUT = (10, 30)


class MailgunEmailProvider(EmailMaster):
    """Send RFQ emails through the Mailgun messages API.

    Inherits all address and template helpers from :class:`EmailMaster` and
    adds Mailgun-specific transmission via :mod:`requests`.

    Attributes:
        settings (Settings): Shared application configuration.
        log (logging.Logger): Shared application logger.
        api_key (str): Validated Mailgun private API key.
        domain (str): Validated Mailgun sending domain.
        messages_url (str): Fully built ``.../messages`` endpoint URL.

    Example:
        >>> provider = MailgunEmailProvider(settings, logger)
        >>> result = provider.send_email(            # doctest: +SKIP
        ...     from_email="noreply@yourdomain.com", from_name="Acme",
        ...     to_email="buyer@x.com", to_name="Buyer",
        ...     subject="[RFQ - hd273hsd] - Hi", html_body="<p>Hi</p>",
        ...     message_id="<rfq.hd273hsd.ab@mail.yourdomain.com>")
        >>> result["provider"]                       # doctest: +SKIP
        'mailgun'
    """

    def __init__(
        self, settings: Settings, logger: logging.Logger
    ) -> None:
        """Validate credentials and pre-build the messages endpoint URL.

        Args:
            settings (Settings): Shared application configuration.
            logger (logging.Logger): Shared application logger.

        Returns:
            None

        Raises:
            ProviderConfigError: If ``MAILGUN_API_KEY`` or
                ``MAILGUN_DOMAIN`` is not set.
        """
        super().__init__(settings, logger)
        self.api_key = self._require(
            settings.mailgun_api_key, "MAILGUN_API_KEY"
        )
        self.domain = self._require(
            settings.mailgun_domain, "MAILGUN_DOMAIN"
        )
        self.messages_url = (
            f"{settings.mailgun_api_base}/v3/{self.domain}/messages"
        )
        self.log.info(
            "Mailgun provider initialised (domain=%s)", self.domain
        )

    @property
    def provider_name(self) -> str:
        """Return the provider key ``"mailgun"``.

        Returns:
            str: Always ``"mailgun"``.
        """
        return "mailgun"

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
        """Send one email via the Mailgun messages API.

        Submits an HTTP Basic-authenticated ``POST`` with the standard
        Mailgun form fields. A ``2xx`` response means Mailgun queued the
        message; the response JSON contains the Mailgun message id. No
        ``Reply-To`` is sent — replies thread on ``Message-ID``/``References``
        and the ``[RFQ - id]`` subject prefix.

        Args:
            from_email (str): Verified sender address.
            from_name (str): Sender display name.
            to_email (str): Recipient address.
            to_name (str): Recipient display name.
            subject (str): Subject line.
            html_body (str): HTML body.
            message_id (str): The RFC ``Message-ID`` this app minted, sent as
                the ``h:Message-Id`` form field.
            extra_headers (dict[str, str] | None): Extra headers, each sent
                as its own ``h:{name}`` field.
            attachments (list | None): Optional attachments.

        Returns:
            dict: ``{"status_code": int, "provider": "mailgun",
                "provider_message_id": str | None, "message_id": str}``.

        Raises:
            EmailSendError: If the request fails at the network level or
                Mailgun returns a non-2xx status.

        Example:
            >>> provider.send_email(                  # doctest: +SKIP
            ...     from_email="noreply@yourdomain.com",
            ...     from_name="Acme", to_email="buyer@x.com",
            ...     to_name="Buyer", subject="[RFQ - hd273hsd] - Hi",
            ...     html_body="<p>Hi</p>",
            ...     message_id="<rfq.hd273hsd.ab@mail.yourdomain.com>")
            {'status_code': 200, 'provider': 'mailgun', ...}
        """
        # Mailgun accepts a combined "Display Name <address>" recipient.
        from_header = f"{from_name} <{from_email}>"
        to_header = f"{to_name} <{to_email}>" if to_name else to_email
        payload = {
            "from": from_header,
            "to": to_header,
            "subject": subject,
            "html": html_body,
            # ``h:`` prefixed fields are passed through as raw headers.
            "h:Message-Id": message_id,
        }
        for key, value in (extra_headers or {}).items():
            payload[f"h:{key}"] = value

        files = [
            ("attachment", (att["filename"], att["content"],
                            att.get("content_type", "application/octet-stream")))
            for att in (attachments or [])
        ]

        try:
            response = requests.post(
                self.messages_url,
                auth=(_BASIC_AUTH_USER, self.api_key),
                data=payload,
                files=files or None,
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            self.log.error("Mailgun request error: %s", exc)
            raise EmailSendError(
                f"Mailgun network error sending to {to_email}: {exc}"
            ) from exc

        # A non-2xx status is a hard failure — surface Mailgun's own body so
        # the operator sees the real reason (bad domain, unauthorised, ...).
        if not 200 <= response.status_code < 300:
            self.log.error(
                "Mailgun rejected send (status=%s): %s",
                response.status_code,
                response.text[:300],
            )
            raise EmailSendError(
                f"Mailgun rejected email to {to_email} "
                f"(status {response.status_code}): {response.text[:200]}"
            )

        # Mailgun's own id for the queued message, distinct from the RFC
        # Message-ID we minted and sent as ``h:Message-Id``.
        provider_message_id = None
        try:
            provider_message_id = response.json().get("id")
        except ValueError:
            # Non-JSON body is unexpected but not fatal; the send succeeded.
            self.log.warning("Mailgun returned a non-JSON success body")

        self.log.info(
            "Mailgun accepted email to %s (status=%s)",
            to_email,
            response.status_code,
        )
        return {
            "status_code": response.status_code,
            "provider": self.provider_name,
            "provider_message_id": provider_message_id,
            "message_id": message_id,
        }
