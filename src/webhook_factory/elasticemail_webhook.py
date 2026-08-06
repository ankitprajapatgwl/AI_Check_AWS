"""Inbound webhook parser for Elastic Email inbound notifications.

Elastic Email's inbound feature ("Notification Settings" → "To an HTTP
URL") HTTP-POSTs received mail as **form data** (not JSON, and not
SendGrid's multipart field names). This parser maps Elastic Email's field
names onto a normalised :class:`InboundEmail`.

Verified Elastic Email inbound form fields:

==================== ============================================
Field                Meaning
==================== ============================================
``from_email``       Sender address
``from_name``        Sender display name
``env_from``         SMTP envelope sender
``env_to_list``      Envelope recipients
``to_list``          ``To`` header recipients
``subject``          Subject line
``body_text``        Plain-text body
``body_html``        HTML body
``header_list``      Raw headers as ``"Name: Value"`` lines
``att{N}_name``      Attachment N filename (``att1_name`` ...)
``att{N}_content``   Attachment N bytes, **base64-encoded**
==================== ============================================

See ``setup_docs/elasticemail_setup.md`` for the full field reference and
the (important) requirement that the endpoint also answer a ``GET`` probe.

Example:
    >>> from src.webhook_factory.elasticemail_webhook import (
    ...     ElasticEmailWebhookParser)
    >>> parser = ElasticEmailWebhookParser(settings, logger)   # noqa
    >>> inbound = await parser.parse(request)             # doctest: +SKIP
"""

import base64
import email
from email.policy import default

from fastapi import Request

from src.webhook_factory.webhook_master import (
    InboundEmail,
    RawAttachment,
    WebhookParseError,
    WebhookParserMaster,
)


class ElasticEmailWebhookParser(WebhookParserMaster):
    """Parse Elastic Email inbound-notification form payloads.

    Inherits attachment persistence and the default (trusted) signature
    check from :class:`WebhookParserMaster`.

    Example:
        >>> parser = ElasticEmailWebhookParser(settings, logger)
        >>> parser.provider_name
        'elasticemail'
    """

    @property
    def provider_name(self) -> str:
        """Return the provider key ``"elasticemail"``.

        Returns:
            str: Always ``"elasticemail"``.
        """
        return "elasticemail"

    async def parse(self, request: Request) -> InboundEmail:
        """Extract a normalised inbound email from an Elastic Email POST.

        Reads the form payload, combines the recipient lists into one string
        (so the dynamic-address regex can locate the
        ``usr..._conv...@domain`` value wherever Elastic Email placed it),
        and base64-decodes any numbered attachment fields.

        Args:
            request (Request): The FastAPI request for the inbound POST.

        Returns:
            InboundEmail: The normalised inbound email with
                ``provider="elasticemail"``.

        Raises:
            WebhookParseError: If the form payload cannot be read.

        Example:
            >>> inbound = await parser.parse(request)   # doctest: +SKIP
            >>> inbound.provider                        # doctest: +SKIP
            'elasticemail'
        """
        try:
            form = await request.form()
        except Exception as exc:  # noqa: BLE001 - normalise to one type
            raise WebhookParseError(
                f"Could not read Elastic Email form body: {exc}"
            ) from exc

        # Either recipient list may hold the address the supplier actually
        # replied to, so both are joined and recorded.
        to_combined = " ".join(
            value
            for value in (
                form.get("to_list", ""),
                form.get("env_to_list", ""),
            )
            if value
        )

        # ``header_list`` is a raw "Name: Value" block; parsing it as a
        # headers-only MIME message recovers the threading headers.
        raw_headers = form.get("header_list", "") or ""
        headers = self.headers_from_mime(
            email.message_from_string(raw_headers, policy=default)
            if raw_headers
            else None
        )

        inbound = InboundEmail(
            from_email=form.get("from_email", ""),
            to_email=to_combined,
            subject=form.get("subject", ""),
            body_text=form.get("body_text", ""),
            body_html=form.get("body_html", ""),
            provider=self.provider_name,
            message_id=headers.get("message-id", ""),
            in_reply_to=headers.get("in-reply-to", ""),
            references=headers.get("references", ""),
            headers=headers,
            raw_message=raw_headers,
        )

        inbound.attachments = self._extract_attachments(form)
        self.log.info(
            "Parsed Elastic Email inbound: %s -> %s (%d attachment(s), "
            "message_id=%s)",
            inbound.from_email,
            to_combined,
            len(inbound.attachments),
            inbound.message_id or "-",
        )
        return inbound

    def _extract_attachments(self, form) -> list[RawAttachment]:
        """Decode Elastic Email's numbered base64 attachment fields.

        Iterates ``att1_name`` / ``att1_content``, ``att2_*`` ... until a
        gap is found, base64-decoding each ``att{N}_content`` value.

        Args:
            form: The parsed form mapping.

        Returns:
            list[RawAttachment]: One entry per decodable attachment; empty
                when the message carried none.
        """
        attachments: list[RawAttachment] = []
        index = 1
        while True:
            name = form.get(f"att{index}_name")
            encoded = form.get(f"att{index}_content")
            if name is None and encoded is None:
                # No more numbered attachment fields.
                break
            if encoded:
                try:
                    content = base64.b64decode(encoded)
                except (ValueError, TypeError) as exc:
                    self.log.error(
                        "Could not base64-decode att%s_content: %s",
                        index,
                        exc,
                    )
                    content = b""
                attachments.append(
                    RawAttachment(
                        name or f"attachment_{index}",
                        "application/octet-stream",
                        content,
                    )
                )
            index += 1
        return attachments
