"""Inbound adapter for Alibaba Enterprise Mail — MIME in, InboundEmail out.

Alibaba Enterprise Mail has **no inbound webhook**. Replies are fetched by
polling the mailbox over IMAP (see
:class:`~src.inbound.alibaba_imap_poller.AlibabaImapPoller`), so this class
is not an HTTP parser at all: it converts an already-fetched
:class:`email.message.EmailMessage` into the same
:class:`~src.webhook_factory.webhook_master.InboundEmail` every other
provider produces, and the service layer's matching pipeline then treats an
Alibaba reply exactly like a webhook-delivered one.

It still subclasses :class:`WebhookParserMaster` and registers in
:mod:`src.webhook_factory.factory` so the factory stays uniform (and so it
inherits the attachment persistence and MIME helpers). Its :meth:`parse`
deliberately raises — reaching it means an HTTP POST to
``/webhooks/rfq/inbound`` was resolved to Alibaba, which cannot happen for
real Alibaba mail (it arrives over IMAP, never as a webhook).

Example:
    >>> import email
    >>> adapter = AlibabaWebhookParser(settings, logger)   # doctest: +SKIP
    >>> msg = email.message_from_bytes(raw)                # doctest: +SKIP
    >>> inbound = adapter.from_mime(msg, raw.decode())     # doctest: +SKIP
    >>> inbound.provider                                   # doctest: +SKIP
    'alibaba'
"""

from email.message import Message
from email.utils import parseaddr

from fastapi import Request

from src.webhook_factory.webhook_master import (
    InboundEmail,
    WebhookParseError,
    WebhookParserMaster,
)


class AlibabaWebhookParser(WebhookParserMaster):
    """Turn a fetched MIME message into a normalised :class:`InboundEmail`.

    Example:
        >>> parser = AlibabaWebhookParser(settings, logger)
        >>> parser.provider_name
        'alibaba'
    """

    @property
    def provider_name(self) -> str:
        """Return the provider key ``"alibaba"``.

        Returns:
            str: Always ``"alibaba"``.
        """
        return "alibaba"

    async def parse(self, request: Request) -> InboundEmail:
        """Always raise — Alibaba delivers inbound mail over IMAP, not HTTP.

        Args:
            request (Request): Unused; present to satisfy the base class.

        Returns:
            InboundEmail: Never returns.

        Raises:
            WebhookParseError: Always.
        """
        raise WebhookParseError(
            "Alibaba Enterprise Mail has no inbound webhook; inbound arrives "
            "via IMAP polling (see src.inbound.alibaba_imap_poller)."
        )

    def from_mime(self, msg: Message, raw: str = "") -> InboundEmail:
        """Build an :class:`InboundEmail` from a fetched MIME message.

        Args:
            msg (Message): The parsed message, ideally read with
                ``policy=email.policy.default`` so encoded-word headers
                (Chinese subjects) are already decoded.
            raw (str): The full raw source, retained on the result for
                debugging and for the unmatched-email record.

        Returns:
            InboundEmail: Normalised, with ``provider="alibaba"``. The caller
                (the poller) overwrites ``provider`` with the region key it
                is polling for.
        """
        headers = self.headers_from_mime(msg)
        body_text, body_html = self._bodies(msg)

        inbound = InboundEmail(
            from_email=self._address(headers.get("from", "")),
            to_email=self._address(headers.get("to", "")),
            subject=headers.get("subject", ""),
            body_text=body_text,
            body_html=body_html,
            # IMAP is an authenticated pull from our own mailbox, so there is
            # no webhook payload whose authenticity needs proving.
            signature_verified=True,
            dkim=headers.get("authentication-results", ""),
            spf=headers.get("received-spf", ""),
            provider=self.provider_name,
            # Stripped because :mod:`email.policy.default` parses these as
            # structured headers and keeps the folding whitespace that follows
            # the colon — ``" <id@host>"``. ``message_id`` is the inbound
            # idempotency key and gets stored verbatim, so a stray leading
            # space would make the same message look different depending on
            # which path delivered it.
            message_id=headers.get("message-id", "").strip(),
            in_reply_to=headers.get("in-reply-to", "").strip(),
            references=headers.get("references", "").strip(),
            headers=headers,
            raw_message=raw,
        )
        inbound.attachments = self.attachments_from_mime(msg)

        self.log.info(
            "Parsed Alibaba inbound: %s -> %s (%d attachment(s), "
            "message_id=%s)",
            inbound.from_email,
            inbound.to_email,
            len(inbound.attachments),
            inbound.message_id or "-",
        )
        return inbound

    @staticmethod
    def _address(raw: str) -> str:
        """Reduce a ``"Name <addr>"`` header value to the bare address.

        Args:
            raw (str): The raw header value.

        Returns:
            str: The bare address, or ``raw`` when it cannot be parsed.
        """
        return parseaddr(raw or "")[1] or (raw or "")

    @staticmethod
    def _bodies(msg: Message) -> tuple[str, str]:
        """Pull the plain-text and HTML bodies out of a MIME message.

        Attachment parts are skipped even when they are ``text/*`` — an
        attached ``.txt`` is not the message body.

        Args:
            msg (Message): The parsed message.

        Returns:
            tuple[str, str]: ``(body_text, body_html)``; either may be ``""``.
        """
        if not msg.is_multipart():
            payload = msg.get_payload(decode=True) or b""
            text = payload.decode(msg.get_content_charset() or "utf-8", "replace")
            return ("", text) if msg.get_content_type() == "text/html" else (text, "")

        body_text, body_html = "", ""
        for part in msg.walk():
            if part.is_multipart() or part.get_content_disposition() == "attachment":
                continue
            content_type = part.get_content_type()
            if content_type not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True) or b""
            decoded = payload.decode(
                part.get_content_charset() or "utf-8", "replace"
            )
            # First part of each kind wins — later ones are usually quoted
            # duplicates from forwarded/nested messages.
            if content_type == "text/plain" and not body_text:
                body_text = decoded
            elif content_type == "text/html" and not body_html:
                body_html = decoded
        return body_text, body_html
