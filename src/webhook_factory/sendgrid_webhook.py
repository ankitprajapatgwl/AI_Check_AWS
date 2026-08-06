"""Inbound webhook parser for SendGrid Inbound Parse.

SendGrid's Inbound Parse webhook POSTs ``multipart/form-data`` for every
message delivered to ``*@{SENDGRID_OUTBOUND_DOMAIN}``. This parser converts
that payload into a normalised :class:`InboundEmail`.

Relevant SendGrid form fields:

============== ==================================================
Field          Meaning
============== ==================================================
``from``       Sender address
``to``         Recipient (the dynamic conversation address)
``subject``    Subject line
``text``       Plain-text body
``html``       HTML body
``spam_score`` SpamAssassin score (string)
``SPF``        SPF result
``dkim``       DKIM result
``headers``    Raw header block of the original message
``attachments`` Number of attachments (string integer)
``attachment-info`` JSON map of per-attachment metadata
``attachment1``..``attachmentN`` Uploaded files
============== ==================================================

``headers`` is what carries ``Message-ID``/``In-Reply-To``/``References``,
so it is parsed as a headers-only MIME message to drive inbound threading.

Example:
    >>> from src.webhook_factory.sendgrid_webhook import (
    ...     SendGridWebhookParser)
    >>> parser = SendGridWebhookParser(settings, logger)  # doctest: +SKIP
    >>> inbound = await parser.parse(request)             # doctest: +SKIP
"""

import email
import json
from email.policy import default

from fastapi import Request

from src.webhook_factory.webhook_master import (
    InboundEmail,
    RawAttachment,
    WebhookParseError,
    WebhookParserMaster,
)


class SendGridWebhookParser(WebhookParserMaster):
    """Parse SendGrid Inbound Parse multipart payloads.

    Inherits attachment persistence and the default (trusted) signature
    check from :class:`WebhookParserMaster`.

    Example:
        >>> parser = SendGridWebhookParser(settings, logger)
        >>> parser.provider_name
        'sendgrid'
    """

    @property
    def provider_name(self) -> str:
        """Return the provider key ``"sendgrid"``.

        Returns:
            str: Always ``"sendgrid"``.
        """
        return "sendgrid"

    async def parse(self, request: Request) -> InboundEmail:
        """Extract a normalised inbound email from a SendGrid payload.

        Reads the multipart form, maps SendGrid's field names onto
        :class:`InboundEmail` and loads any attachment bytes into memory
        using the ``attachment-info`` JSON for filenames and MIME types.

        Args:
            request (Request): The FastAPI request for the inbound POST.

        Returns:
            InboundEmail: The normalised inbound email with
                ``provider="sendgrid"``.

        Raises:
            WebhookParseError: If the multipart form cannot be read.

        Example:
            >>> inbound = await parser.parse(request)   # doctest: +SKIP
            >>> inbound.provider                        # doctest: +SKIP
            'sendgrid'
        """
        try:
            form = await request.form()
        except Exception as exc:  # noqa: BLE001 - normalise to one type
            raise WebhookParseError(
                f"Could not read SendGrid multipart form: {exc}"
            ) from exc

        spam_raw = form.get("spam_score", "0") or "0"
        try:
            spam_score = float(spam_raw)
        except (TypeError, ValueError):
            spam_score = 0.0

        # Inbound Parse posts the original header block verbatim in a
        # ``headers`` field — parsing it as a headers-only MIME message is
        # the simplest way to get Message-ID/In-Reply-To/References back.
        raw_headers = form.get("headers", "") or ""
        headers = self.headers_from_mime(
            email.message_from_string(raw_headers, policy=default)
            if raw_headers
            else None
        )

        inbound = InboundEmail(
            from_email=form.get("from", ""),
            to_email=form.get("to", ""),
            subject=form.get("subject", ""),
            body_text=form.get("text", ""),
            body_html=form.get("html", ""),
            spam_score=spam_score,
            dkim=form.get("dkim", ""),
            spf=form.get("SPF", ""),
            provider=self.provider_name,
            message_id=headers.get("message-id", ""),
            in_reply_to=headers.get("in-reply-to", ""),
            references=headers.get("references", ""),
            headers=headers,
            raw_message=raw_headers,
        )

        inbound.attachments = await self._extract_attachments(form)
        self.log.info(
            "Parsed SendGrid inbound: %s -> %s (%d attachment(s), "
            "message_id=%s)",
            inbound.from_email,
            inbound.to_email,
            len(inbound.attachments),
            inbound.message_id or "-",
        )
        return inbound

    async def _extract_attachments(self, form) -> list[RawAttachment]:
        """Read SendGrid attachment files into memory.

        Args:
            form: The parsed multipart form mapping.

        Returns:
            list[RawAttachment]: One entry per uploaded file; empty when the
                message carried no attachments.
        """
        count = int(form.get("attachments", 0) or 0)
        if count <= 0:
            return []

        try:
            info = json.loads(form.get("attachment-info", "{}") or "{}")
        except (TypeError, ValueError):
            # Missing/garbled metadata is non-fatal — fall back to defaults.
            info = {}

        attachments: list[RawAttachment] = []
        for i in range(1, count + 1):
            upload = form.get(f"attachment{i}")
            if not upload or not hasattr(upload, "read"):
                continue
            meta = info.get(f"attachment{i}", {})
            filename = meta.get("filename", f"attachment_{i}")
            content_type = meta.get("type", "application/octet-stream")
            content = await upload.read()
            attachments.append(
                RawAttachment(filename, content_type, content)
            )
        if len(attachments) != count:
            # The declared count and the actual uploaded parts disagree —
            # not fatal, but worth surfacing when debugging missing files.
            self.log.debug(
                "SendGrid declared %d attachment(s) but %d were present",
                count,
                len(attachments),
            )
        return attachments
