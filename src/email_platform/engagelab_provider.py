"""Outbound email provider backed by the EngageLab Email API.

:class:`EngageLabEmailProvider` implements :meth:`EmailMaster.send_email` by
POSTing JSON to EngageLab's ``/v1/mail/send`` endpoint with the
:mod:`requests` library — EngageLab does not ship a first-party Python SDK.
Unlike SendCloud/Mailgun (``multipart/form-data``), EngageLab's Trigger
Email API takes a plain JSON body. Authentication is HTTP Basic Auth using
``api_user``/``api_key`` (NOT the EngageLab login email) — ``requests``
builds the ``Authorization: Basic base64(api_user:api_key)`` header when
given ``auth=(api_user, api_key)``.

Because the verified subdomain (``ENGAGELAB_OUTBOUND_DOMAIN``) is fully
authenticated, EngageLab allows the local-part of the ``from`` address to be
defined dynamically at send time with no per-address pre-registration — see
:meth:`EmailMaster.build_sending_email`.

Threading rides on ``body.headers``: the RFC ``Message-ID`` this app mints
plus ``X-RFQ-Conversation-Id`` are passed through there. EngageLab documents
that object as capped at 1 KB with a forbidden-key list that does not include
``Message-ID`` (``setup_docs/engagelab_guide/email_send_guice.md:87``). No
``reply_to`` is sent at all.

Configuration consumed (see :class:`src.config.Settings`):

- ``ENGAGELAB_API_USER`` *(required)* – API_USER created in the EngageLab
  dashboard (Send Settings → API_USER), bound to the sending subdomain.
- ``ENGAGELAB_API_KEY`` *(required)* – API_KEY generated for that API_USER.
- ``ENGAGELAB_API_BASE`` – region base URL (Singapore by default).
- ``ENGAGELAB_OUTBOUND_DOMAIN`` *(required)* – the verified sending
  subdomain, used to build the ``From`` address for sends made through
  EngageLab.
- ``ENGAGELAB_COMPANY_NAME`` – display name in the ``from`` header (defaults
  to ``"Your Company"``).

Example:
    >>> from src.email_platform.engagelab_provider import (
    ...     EngageLabEmailProvider)
    >>> from src.config import get_settings
    >>> from src.logger import AppLogger
    >>> provider = EngageLabEmailProvider(get_settings(), AppLogger.get())
    >>> provider.provider_name                        # doctest: +SKIP
    'engagelab'
"""

import base64
import logging

import requests

from src.config import Settings
from src.email_platform.email_master import EmailMaster, EmailSendError

# Network timeout (connect, read) in seconds for the send request.
_REQUEST_TIMEOUT = (10, 30)

# ``0`` selects individual transactional sending (vs. batch) per the
# EngageLab Trigger Email API.
_SEND_MODE_TRANSACTIONAL = 0


class EngageLabEmailProvider(EmailMaster):
    """Send RFQ emails through the EngageLab Trigger Email API.

    Inherits all address and template helpers from :class:`EmailMaster` and
    adds EngageLab-specific transmission via :mod:`requests`.

    Attributes:
        settings (Settings): Shared application configuration.
        log (logging.Logger): Shared application logger.
        api_user (str): Validated EngageLab ``API_USER`` credential.
        api_key (str): Validated EngageLab ``API_KEY`` credential.
        send_url (str): Fully built ``.../v1/mail/send`` endpoint URL.

    Example:
        >>> provider = EngageLabEmailProvider(settings, logger)
        >>> result = provider.send_email(              # doctest: +SKIP
        ...     from_email="JamesWhitfield@mail.jobsetu.online",
        ...     from_name="Acme", to_email="buyer@x.com", to_name="Buyer",
        ...     subject="[RFQ - hd273hsd] - Hi", html_body="<p>Hi</p>",
        ...     message_id="<rfq.hd273hsd.ab@mail.jobsetu.online>")
        >>> result["provider"]                         # doctest: +SKIP
        'engagelab'
    """

    def __init__(
        self, settings: Settings, logger: logging.Logger
    ) -> None:
        """Validate credentials and pre-build the send endpoint URL.

        Args:
            settings (Settings): Shared application configuration.
            logger (logging.Logger): Shared application logger.

        Returns:
            None

        Raises:
            ProviderConfigError: If ``ENGAGELAB_API_USER`` or
                ``ENGAGELAB_API_KEY`` is not set.
        """
        super().__init__(settings, logger)
        self.api_user = self._require(
            settings.engagelab_api_user, "ENGAGELAB_API_USER"
        )
        self.api_key = self._require(
            settings.engagelab_api_key, "ENGAGELAB_API_KEY"
        )
        self.send_url = f"{settings.engagelab_api_base}/v1/mail/send"
        self.log.info("EngageLab provider initialised")

    @property
    def provider_name(self) -> str:
        """Return the provider key ``"engagelab"``.

        Returns:
            str: Always ``"engagelab"``.
        """
        return "engagelab"

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
        """Send one email via the EngageLab ``POST /v1/mail/send`` endpoint.

        Submits a JSON ``POST`` authenticated with HTTP Basic Auth
        (``api_user``/``api_key``). No ``reply_to`` is sent — replies are
        threaded by ``Message-ID``/``References`` and the ``[RFQ - id]``
        subject prefix, so a reply simply goes back to ``from``.

        Args:
            from_email (str): Sender address for the ``from`` header (suffix
                must match the verified ``ENGAGELAB_OUTBOUND_DOMAIN``).
            from_name (str): Display name for the ``from`` header.
            to_email (str): Recipient address.
            to_name (str): Recipient display name (unused by the API but
                accepted for interface parity).
            subject (str): Subject line.
            html_body (str): HTML body.
            message_id (str): The RFC ``Message-ID`` this app minted, passed
                through EngageLab's ``body.headers`` object.
            extra_headers (dict[str, str] | None): Extra headers merged into
                the same object, e.g. ``X-RFQ-Conversation-Id``.
            attachments (list | None): Optional attachments, each base64
                encoded into the ``body.attachments`` array per EngageLab's
                documented schema (``filename``, ``type``, ``content``,
                ``disposition``).

        Returns:
            dict: ``{"status_code": int, "provider": "engagelab",
                "provider_message_id": str | None, "message_id": str}``.

        Raises:
            EmailSendError: If the request fails at the network level, the
                HTTP status is non-2xx, or the response body is unparseable.

        Example:
            >>> provider.send_email(                   # doctest: +SKIP
            ...     from_email="JamesWhitfield@mail.jobsetu.online",
            ...     from_name="Acme", to_email="buyer@x.com",
            ...     to_name="Buyer", subject="[RFQ - hd273hsd] - Hi",
            ...     html_body="<p>Hi</p>",
            ...     message_id="<rfq.hd273hsd.ab@mail.jobsetu.online>")
            {'status_code': 200, 'provider': 'engagelab', ...}
        """
        mail_body: dict = {
            "subject": subject,
            "content": {"html": html_body},
            # EngageLab documents a `headers` object (max 1 KB) whose
            # forbidden-key list does not include Message-ID — see
            # setup_docs/engagelab_guide/email_send_guice.md:87. If a future
            # API change starts rewriting it, matching degrades to the
            # subject token, which is still correct.
            "headers": {"Message-ID": message_id, **(extra_headers or {})},
            "settings": {
                "send_mode": _SEND_MODE_TRANSACTIONAL,
                "return_email_id": True,
            },
        }

        if attachments:
            mail_body["attachments"] = [
                {
                    "filename": att["filename"],
                    "type": att.get(
                        "content_type", "application/octet-stream"
                    ),
                    "content": base64.b64encode(
                        att["content"]
                    ).decode("ascii"),
                    "disposition": "attachment",
                }
                for att in attachments
            ]

        payload = {
            "from": f"{from_name} <{from_email}>",
            "to": [to_email],
            "body": mail_body,
        }

        try:
            response = requests.post(
                self.send_url,
                json=payload,
                auth=(self.api_user, self.api_key),
                headers={"Accept": "application/json"},
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            self.log.error("EngageLab request error: %s", exc)
            raise EmailSendError(
                f"EngageLab network error sending to {to_email}: {exc}"
            ) from exc

        # A non-2xx status is a hard failure — surface EngageLab's own body
        # so the operator sees the real reason (bad auth, bad params, ...).
        if not 200 <= response.status_code < 300:
            self.log.error(
                "EngageLab rejected send (status=%s): %s",
                response.status_code,
                response.text[:300],
            )
            raise EmailSendError(
                f"EngageLab rejected email to {to_email} "
                f"(status {response.status_code}): {response.text[:200]}"
            )

        provider_message_id = None
        try:
            body = response.json()
        except ValueError:
            # Non-JSON body is unexpected but not fatal; the send succeeded.
            self.log.warning("EngageLab returned a non-JSON success body")
            body = {}

        # Per EngageLab's documented response schema, individual sends
        # return "email_ids" (one per recipient); address-list sends
        # (send_mode=2) return "task_id" instead. This is EngageLab's own id,
        # kept separate from the RFC Message-ID we minted and sent.
        email_ids = body.get("email_ids")
        if isinstance(email_ids, list) and email_ids:
            provider_message_id = email_ids[0]
        elif body.get("task_id"):
            provider_message_id = body["task_id"]
        elif body.get("request_id"):
            provider_message_id = body["request_id"]

        self.log.info(
            "EngageLab accepted email to %s (status=%s)",
            to_email,
            response.status_code,
        )
        return {
            "status_code": response.status_code,
            "provider": self.provider_name,
            "provider_message_id": provider_message_id,
            "message_id": message_id,
        }
