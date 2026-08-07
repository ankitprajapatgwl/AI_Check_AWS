"""Inbound webhook parser for Mailgun Routes (inbound forwarding).

A Mailgun *route* with a ``forward("https://.../webhooks/rfq/inbound")`` action
POSTs the parsed message as ``multipart/form-data``. This parser converts
that payload into a normalised :class:`InboundEmail` and verifies the
request's authenticity with Mailgun's HMAC signature scheme.

Relevant Mailgun form fields:

==================== ============================================
Field                Meaning
==================== ============================================
``sender``           Sender address
``recipient``        Recipient (the dynamic conversation address)
``subject``          Subject line
``body-plain``       Plain-text body
``body-html``        HTML body
``message-headers``  JSON array of ``[name, value]`` header pairs
``Message-Id``       Flattened copy of the same header
``In-Reply-To``      Flattened copy — the primary threading signal
``References``       Flattened copy — the secondary threading signal
``attachment-count`` Number of attachments (string integer)
``attachment-1``..N  Uploaded files
``timestamp``        Signature timestamp
``token``            Signature token
``signature``        HMAC-SHA256 over ``{timestamp}{token}``
``X-Mailgun-Sscore`` Spam score (when present)
==================== ============================================

Example:
    >>> from src.webhook_factory.mailgun_webhook import (
    ...     MailgunWebhookParser)
    >>> parser = MailgunWebhookParser(settings, logger)   # doctest: +SKIP
    >>> inbound = await parser.parse(request)             # doctest: +SKIP
"""

import hashlib
import hmac
import json

from fastapi import Request

from src.webhook_factory.webhook_master import (
    InboundEmail,
    RawAttachment,
    WebhookParseError,
    WebhookParserMaster,
)


class MailgunWebhookParser(WebhookParserMaster):
    """Parse Mailgun inbound-route multipart payloads.

    Overrides :meth:`verify_signature` to validate Mailgun's
    ``timestamp``/``token``/``signature`` triple. Inherits attachment
    persistence from :class:`WebhookParserMaster`.

    Example:
        >>> parser = MailgunWebhookParser(settings, logger)
        >>> parser.provider_name
        'mailgun'
    """

    @property
    def provider_name(self) -> str:
        """Return the provider key ``"mailgun"``.

        Returns:
            str: Always ``"mailgun"``.
        """
        return "mailgun"

    async def parse(self, request: Request) -> InboundEmail:
        """Extract a normalised inbound email from a Mailgun payload.

        Reads the multipart form, verifies the HMAC signature, maps
        Mailgun's field names onto :class:`InboundEmail` and loads
        attachment bytes into memory.

        Args:
            request (Request): The FastAPI request for the inbound POST.

        Returns:
            InboundEmail: The normalised inbound email with
                ``provider="mailgun"`` and
                :attr:`InboundEmail.signature_verified` set from the HMAC
                check.

        Raises:
            WebhookParseError: If the multipart form cannot be read.

        Example:
            >>> inbound = await parser.parse(request)   # doctest: +SKIP
            >>> inbound.provider                        # doctest: +SKIP
            'mailgun'
        """
        try:
            form = await request.form()
        except Exception as exc:  # noqa: BLE001 - normalise to one type
            raise WebhookParseError(
                f"Could not read Mailgun multipart form: {exc}"
            ) from exc

        verified = self._verify_form_signature(
            timestamp=form.get("timestamp", ""),
            token=form.get("token", ""),
            signature=form.get("signature", ""),
        )

        # Mailgun does not expose dedicated spam/SPF/DKIM form fields; the
        # canonical source is the JSON-encoded ``message-headers`` list. Fall
        # back to any flattened top-level fields if the header is absent.
        headers = self._parse_message_headers(
            form.get("message-headers", "")
        )
        spam_score = self._to_float(
            headers.get("x-mailgun-sscore")
            or form.get("X-Mailgun-Sscore")
        )

        # Mailgun flattens the common threading headers into their own form
        # fields as well as listing them in message-headers; prefer the
        # parsed header list and fall back to the flat fields.
        message_id = headers.get("message-id") or form.get("Message-Id", "")
        in_reply_to = headers.get("in-reply-to") or form.get("In-Reply-To", "")
        references = headers.get("references") or form.get("References", "")

        inbound = InboundEmail(
            from_email=form.get("sender", ""),
            to_email=form.get("recipient", ""),
            subject=form.get("subject", ""),
            body_text=form.get("body-plain", ""),
            body_html=form.get("body-html", ""),
            spam_score=spam_score,
            dkim=headers.get("authentication-results", ""),
            spf=headers.get("received-spf", ""),
            signature_verified=verified,
            provider=self.provider_name,
            message_id=message_id,
            in_reply_to=in_reply_to,
            references=references,
            headers=headers,
        )

        inbound.attachments = await self._extract_attachments(form)
        self.log.info(
            "Parsed Mailgun inbound: %s -> %s (verified=%s, %d att., "
            "message_id=%s)",
            inbound.from_email,
            inbound.to_email,
            verified,
            len(inbound.attachments),
            inbound.message_id or "-",
        )
        return inbound

    async def _extract_attachments(self, form) -> list[RawAttachment]:
        """Read Mailgun attachment files into memory.

        Args:
            form: The parsed multipart form mapping.

        Returns:
            list[RawAttachment]: One entry per uploaded file; empty when the
                message carried no attachments.
        """
        count = int(form.get("attachment-count", 0) or 0)
        attachments: list[RawAttachment] = []
        for i in range(1, count + 1):
            upload = form.get(f"attachment-{i}")
            if not upload or not hasattr(upload, "read"):
                continue
            filename = getattr(upload, "filename", f"attachment_{i}")
            content_type = (
                getattr(upload, "content_type", None)
                or "application/octet-stream"
            )
            content = await upload.read()
            attachments.append(
                RawAttachment(filename, content_type, content)
            )
        return attachments

    @staticmethod
    def _parse_message_headers(raw: str) -> dict:
        """Parse Mailgun's ``message-headers`` JSON into a lookup dict.

        Mailgun sends ``message-headers`` as a JSON-encoded list of
        ``[name, value]`` pairs (order preserved). This flattens it into a
        lowercase-keyed dict for easy, case-insensitive lookups.

        Args:
            raw (str): The raw ``message-headers`` form field value.

        Returns:
            dict: Mapping of lowercase header name to value. Empty when the
                field is missing or malformed.

        Example:
            >>> MailgunWebhookParser._parse_message_headers(
            ...     '[["X-Mailgun-Sscore", "1.2"]]')
            {'x-mailgun-sscore': '1.2'}
        """
        if not raw:
            return {}
        try:
            pairs = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        headers: dict = {}
        for pair in pairs:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                headers[str(pair[0]).lower()] = str(pair[1])
        return headers

    @staticmethod
    def _to_float(value) -> float:
        """Coerce a possibly-missing spam-score value to ``float``.

        Args:
            value: The raw value (``str``, ``None`` or numeric).

        Returns:
            float: The parsed score, or ``0.0`` when missing/non-numeric.
        """
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _verify_form_signature(
        self, timestamp: str, token: str, signature: str
    ) -> bool:
        """Verify the Mailgun HMAC signature on an inbound request.

        Mailgun signs inbound POSTs by computing
        ``HMAC-SHA256(key=signing_key, msg=timestamp + token)`` and sending
        the hex digest as ``signature``. We recompute it and compare in
        constant time.

        Args:
            timestamp (str): The ``timestamp`` form field.
            token (str): The ``token`` form field.
            signature (str): The ``signature`` form field (hex digest).

        Returns:
            bool: ``True`` if the signature matches, or if no signing key is
                configured (in which case verification is skipped with a
                warning). ``False`` if a key is configured but the signature
                is missing or wrong.

        Example:
            >>> parser._verify_form_signature("", "", "")  # doctest: +SKIP
            False
        """
        signing_key = self.settings.mailgun_signing_key
        if not signing_key:
            # Without a key we cannot verify; allow it through for local dev
            # but make the security gap visible in the logs.
            self.log.warning(
                "MAILGUN_WEBHOOK_SIGNING_KEY not set — skipping inbound "
                "signature verification"
            )
            return True
        if not (timestamp and token and signature):
            self.log.warning("Mailgun inbound missing signature fields")
            return False

        expected = hmac.new(
            key=signing_key.encode("utf-8"),
            msg=f"{timestamp}{token}".encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
