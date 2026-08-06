"""Outbound email provider backed by the SendCloud (AuroraSendCloud) API.

:class:`SendCloudEmailProvider` implements :meth:`EmailMaster.send_email` by
POSTing ``multipart/form-data`` to SendCloud's ``/api/mail/send`` endpoint
with the :mod:`requests` library — SendCloud does not ship a first-party
Python SDK, so this mirrors the approach already used for
:class:`~src.email_platform.mailgun_provider.MailgunEmailProvider`. The
``From`` header is whatever address the caller passes in —
:meth:`~src.services.conversation_service.ConversationService.send_rfq`
builds it fresh per send via
:meth:`~src.email_platform.email_master.EmailMaster.build_sending_email` on
whichever provider the sender picked. No ``replyTo`` is sent: replies thread
on ``Message-ID``/``References`` and the ``[RFQ - id]`` subject prefix.

.. warning::

   SendCloud's custom-header support is **not documented** in this repo. The
   ``headers`` form field this module sends the ``Message-ID`` through is a
   best guess; the full send response is logged at DEBUG so a live send
   confirms it. If SendCloud strips or rewrites the header, inbound matching
   degrades to the subject token (still correct, just narrower). Confirm
   against docs.aurorasendcloud.com and record the finding in
   ``setup_docs/aurora_send_cloud/AuroraSendCloud_Documentation.md``.

Configuration consumed (see :class:`src.config.Settings`):

- ``SENDCLOUD_API_USER`` *(required)* – API user, from the SendCloud console
  (Singapore region).
- ``SENDCLOUD_API_KEY`` *(required)* – API key, from the SendCloud console
  (Singapore region).
- ``SENDCLOUD_API_BASE`` – region base URL (Singapore by default).
- ``SENDCLOUD_OUTBOUND_DOMAIN`` *(required)* – domain used to build the
  ``From`` address for sends made through SendCloud.
- ``SENDCLOUD_COMPANY_NAME`` – display name in the ``From`` header (defaults
  to ``"Your Company"``).

This module also defines :class:`SendCloudHKEmailProvider`, a Hong Kong/CN
region variant used for Chinese suppliers (see setup_docs/aurora_send_cloud/
AuroraSendCloud_Documentation.md §2) — same behavior, different (region-
locked) credentials and outbound domain: ``SENDCLOUD_HK_API_USER``,
``SENDCLOUD_HK_API_KEY``, ``SENDCLOUD_HK_API_BASE``,
``SENDCLOUD_HK_OUTBOUND_DOMAIN``.

Example:
    >>> from src.email_platform.sendcloud_provider import (
    ...     SendCloudEmailProvider)
    >>> from src.config import get_settings
    >>> from src.logger import AppLogger
    >>> provider = SendCloudEmailProvider(get_settings(), AppLogger.get())
    >>> provider.provider_name                        # doctest: +SKIP
    'sendcloud'
"""

import json
import logging

import requests

from src.config import Settings
from src.email_platform.email_master import EmailMaster, EmailSendError

# Network timeout (connect, read) in seconds for the send request.
_REQUEST_TIMEOUT = (10, 30)


class SendCloudEmailProvider(EmailMaster):
    """Send RFQ emails through the SendCloud ``/api/mail/send`` endpoint.

    Inherits all address and template helpers from :class:`EmailMaster` and
    adds SendCloud-specific transmission via :mod:`requests`.

    Attributes:
        settings (Settings): Shared application configuration.
        log (logging.Logger): Shared application logger.
        api_user (str): Validated SendCloud ``API_USER`` credential.
        api_key (str): Validated SendCloud ``API_KEY`` credential.
        send_url (str): Fully built ``.../api/mail/send`` endpoint URL.

    Example:
        >>> provider = SendCloudEmailProvider(settings, logger)
        >>> result = provider.send_email(             # doctest: +SKIP
        ...     from_email="noreply@yourdomain.com", from_name="Acme",
        ...     to_email="buyer@x.com", to_name="Buyer",
        ...     subject="[RFQ - hd273hsd] - Hi", html_body="<p>Hi</p>",
        ...     message_id="<rfq.hd273hsd.ab@mail.yourdomain.com>")
        >>> result["provider"]                        # doctest: +SKIP
        'sendcloud'
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
            ProviderConfigError: If ``SENDCLOUD_API_USER`` or
                ``SENDCLOUD_API_KEY`` is not set.
        """
        super().__init__(settings, logger)
        self.api_user = self._require(
            settings.sendcloud_api_user, "SENDCLOUD_API_USER"
        )
        self.api_key = self._require(
            settings.sendcloud_api_key, "SENDCLOUD_API_KEY"
        )
        self.send_url = f"{settings.sendcloud_api_base}/api/mail/send"
        self.log.info("SendCloud provider initialised")

    @property
    def provider_name(self) -> str:
        """Return the provider key ``"sendcloud"``.

        Returns:
            str: Always ``"sendcloud"``.
        """
        return "sendcloud"

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
        """Send one email via the SendCloud Basic Send API.

        Submits a ``multipart/form-data`` ``POST`` authenticated with
        ``apiUser``/``apiKey`` included directly in the request body
        (SendCloud does not accept them via HTTP basic auth or headers).
        A response with ``statusCode == 200`` and ``result == true`` means
        SendCloud accepted the message; ``info.emailIdList`` carries the
        provider message id.

        Args:
            from_email (str): Sender address for the ``from`` field — built
                per send via :meth:`EmailMaster.build_sending_email`.
            from_name (str): Sender display name.
            to_email (str): Recipient address.
            to_name (str): Recipient display name (unused by the API but
                accepted for interface parity).
            subject (str): Subject line.
            html_body (str): HTML body.
            message_id (str): The RFC ``Message-ID`` this app minted, sent
                through the ``headers`` form field.
            extra_headers (dict[str, str] | None): Extra headers merged into
                the same field, e.g. ``X-RFQ-Conversation-Id``.
            attachments (list | None): Optional attachments, uploaded as
                multipart file parts.

        Returns:
            dict: ``{"status_code": int, "provider": "sendcloud",
                "provider_message_id": str | None, "message_id": str}``.

        Raises:
            EmailSendError: If the request fails at the network level, the
                HTTP status is non-2xx, or SendCloud reports ``result:
                false``.

        Example:
            >>> provider.send_email(                   # doctest: +SKIP
            ...     from_email="noreply@yourdomain.com",
            ...     from_name="Acme", to_email="buyer@x.com",
            ...     to_name="Buyer", subject="[RFQ - hd273hsd] - Hi",
            ...     html_body="<p>Hi</p>",
            ...     message_id="<rfq.hd273hsd.ab@mail.yourdomain.com>")
            {'status_code': 200, 'provider': 'sendcloud', ...}
        """
        data = {
            "apiUser": self.api_user,
            "apiKey": self.api_key,
            "from": from_email,
            "fromName": from_name,
            "to": to_email,
            "subject": subject,
            "html": html_body,
            # UNVERIFIED against docs.aurorasendcloud.com — SendCloud's
            # custom-header support is undocumented in this repo, so this is
            # a best guess at the field name/encoding. The response is logged
            # in full on send so the first live send confirms or refutes it;
            # if SendCloud drops or rewrites Message-ID, inbound matching
            # simply falls back to the [RFQ - id] subject token.
            "headers": json.dumps(
                {"Message-ID": message_id, **(extra_headers or {})}
            ),
        }

        files = [
            ("attachments", (att["filename"], att["content"],
                             att.get("content_type",
                                     "application/octet-stream")))
            for att in (attachments or [])
        ]
        try:
            response = requests.post(
                self.send_url,
                data=data,
                files=files or None,
                headers={"accept": "application/json"},
                timeout=_REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            self.log.error("SendCloud request error: %s", exc)
            raise EmailSendError(
                f"SendCloud network error sending to {to_email}: {exc}"
            ) from exc

        # A non-2xx status is a hard failure — surface SendCloud's own body
        # so the operator sees the real reason (bad auth, bad params, ...).
        if not 200 <= response.status_code < 300:
            self.log.error(
                "SendCloud rejected send (status=%s): %s",
                response.status_code,
                response.text[:300],
            )
            raise EmailSendError(
                f"SendCloud rejected email to {to_email} "
                f"(status {response.status_code}): {response.text[:200]}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            self.log.error("SendCloud returned a non-JSON body: %s", exc)
            raise EmailSendError(
                f"SendCloud returned an unparseable response for "
                f"{to_email}: {response.text[:200]}"
            ) from exc

        # SendCloud can return HTTP 200 while still failing the send at the
        # application level (``result: false``) — treat that as a failure.
        if not body.get("result"):
            self.log.error(
                "SendCloud send failed for %s: %s", to_email, body
            )
            raise EmailSendError(
                f"SendCloud failed to send email to {to_email}: "
                f"{body.get('message', body)}"
            )

        email_ids = body.get("info", {}).get("emailIdList") or []
        provider_message_id = email_ids[0] if email_ids else None

        self.log.info(
            "SendCloud accepted email to %s (status=%s)",
            to_email,
            body.get("statusCode"),
        )
        # Logged in full (not just the id) so the first live send shows
        # whether the "headers" field above was honoured — see the note on
        # `data["headers"]`.
        self.log.debug("SendCloud send response: %s", body)
        return {
            "status_code": body.get("statusCode", response.status_code),
            "provider": self.provider_name,
            "provider_message_id": provider_message_id,
            "message_id": message_id,
        }


class SendCloudHKEmailProvider(SendCloudEmailProvider):
    """SendCloud, pinned to the Hong Kong/CN region for Chinese mailboxes.

    Registered under the separate factory key ``"sendcloud_hk"`` (see
    :mod:`src.email_platform.factory`) and selected internally by
    :mod:`src.route` when the sender picks SendCloud for a Chinese supplier
    — per setup_docs/aurora_send_cloud/AuroraSendCloud_Documentation.md §2,
    the Hong Kong/CN region is "best used for ... Chinese systems sending to
    Chinese mailboxes (QQ, NetEase)", while the Singapore region (the plain
    ``"sendcloud"`` key) is best for everyone else. Everything about sending
    is identical to :class:`SendCloudEmailProvider` except the credentials,
    base URL and outbound domain, which are region-locked and therefore a
    wholly separate set (``SENDCLOUD_HK_API_USER``/``SENDCLOUD_HK_API_KEY``/
    ``SENDCLOUD_HK_API_BASE``/``SENDCLOUD_HK_OUTBOUND_DOMAIN``).
    :attr:`provider_name` intentionally stays ``"sendcloud"`` (inherited, not
    overridden) so the company name and the conversation's stored
    ``"provider"`` field read the same regardless of which region actually
    sent the email — only the outbound domain differs.

    Example:
        >>> provider = SendCloudHKEmailProvider(settings, logger)
        >>> provider.provider_name                     # doctest: +SKIP
        'sendcloud'
    """

    def __init__(
        self, settings: Settings, logger: logging.Logger
    ) -> None:
        """Validate the Hong Kong region's credentials and build its endpoint.

        Deliberately skips :meth:`EmailMaster.__init__` (and
        :meth:`SendCloudEmailProvider.__init__`), which would resolve
        ``outbound_domain`` from ``SENDCLOUD_OUTBOUND_DOMAIN`` via the shared
        ``"sendcloud"`` :attr:`provider_name` — this region uses its own
        ``SENDCLOUD_HK_OUTBOUND_DOMAIN`` instead.

        Args:
            settings (Settings): Shared application configuration.
            logger (logging.Logger): Shared application logger.

        Returns:
            None

        Raises:
            ProviderConfigError: If ``SENDCLOUD_HK_API_USER``,
                ``SENDCLOUD_HK_API_KEY`` or ``SENDCLOUD_HK_OUTBOUND_DOMAIN``
                is not set.
        """
        self.settings = settings
        self.log = logger
        self.outbound_domain = self._require(
            settings.sendcloud_hk_outbound_domain,
            "SENDCLOUD_HK_OUTBOUND_DOMAIN",
        )
        self.company_name = settings.provider_company_name(self.provider_name)
        self.api_user = self._require(
            settings.sendcloud_hk_api_user, "SENDCLOUD_HK_API_USER"
        )
        self.api_key = self._require(
            settings.sendcloud_hk_api_key, "SENDCLOUD_HK_API_KEY"
        )
        self.send_url = f"{settings.sendcloud_hk_api_base}/api/mail/send"
        self.log.info("SendCloud (Hong Kong/CN region) provider initialised")
