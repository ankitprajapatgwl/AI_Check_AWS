"""Abstract base and shared logic for inbound webhook parsers.

Every email provider posts inbound mail to this app in a slightly different
shape — SendGrid and Mailgun use ``multipart/form-data`` with different
field names, and Elastic Email can post form or JSON. To keep the single
``POST /webhooks/rfq/inbound`` route provider-agnostic, each provider has a
parser that converts its native payload into one normalised
:class:`InboundEmail` object.

:class:`WebhookParserMaster` is the abstract base those parsers inherit.
It owns the behaviour that does not vary by provider:

- :meth:`WebhookParserMaster.persist_attachments` – write extracted
  attachment bytes to disk and return metadata for the UI.
- :meth:`WebhookParserMaster.verify_signature` – a default "trusted"
  implementation that signature-bearing providers (Mailgun) override.

Subclasses implement only :meth:`WebhookParserMaster.parse`, the
provider-specific extraction step.

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
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from fastapi import Request

from src.config import BASE_PATH, Settings


class WebhookParseError(Exception):
    """Raised when an inbound payload cannot be parsed.

    Carries a human-readable message describing what was malformed so the
    webhook route can log it and return a clear error response.
    """


@dataclass
class RawAttachment:
    """An attachment extracted from an inbound payload, still in memory.

    Attributes:
        filename (str): Original filename supplied by the sender.
        content_type (str): MIME type, e.g. ``"application/pdf"``.
        content (bytes): Raw file bytes, ready to be written to disk.
    """

    filename: str
    content_type: str
    content: bytes


@dataclass
class InboundEmail:
    """Normalised inbound email, identical across all providers.

    A parser's :meth:`WebhookParserMaster.parse` returns this object so the
    service layer can process inbound mail without knowing which provider
    produced it. The Alibaba adapter builds one straight from a fetched MIME
    message instead, since that inbound path is an IMAP poll rather than a
    webhook — same object, same downstream pipeline.

    Attributes:
        from_email (str): Sender address.
        to_email (str): Recipient address.
        subject (str): Subject line — carries the ``[RFQ - id]`` prefix when
            the supplier left it intact, which is the fallback match.
        body_text (str): Plain-text body.
        body_html (str): HTML body.
        spam_score (float): Provider spam score (``0.0`` if not supplied).
        dkim (str): DKIM verification result, if provided.
        spf (str): SPF verification result, if provided.
        attachments (list[RawAttachment]): In-memory attachments.
        signature_verified (bool): Whether the payload's authenticity was
            confirmed (always ``True`` for providers that do not sign).
        provider (str): The provider key that produced this payload.
        message_id (str): The sender's own ``Message-ID`` — the inbound
            idempotency key.
        in_reply_to (str): Raw ``In-Reply-To`` header; the primary threading
            signal.
        references (str): Raw ``References`` header; the secondary one.
        headers (dict[str, str]): All headers, lowercase-keyed, so
            ``x-rfq-conversation-id`` can be read back when a client
            preserved it.
        raw_message (str): Full MIME source when the provider supplies one.
    """

    from_email: str = ""
    to_email: str = ""
    subject: str = ""
    body_text: str = ""
    body_html: str = ""
    spam_score: float = 0.0
    dkim: str = ""
    spf: str = ""
    attachments: list[RawAttachment] = field(default_factory=list)
    signature_verified: bool = True
    provider: str = ""
    message_id: str = ""
    in_reply_to: str = ""
    references: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    raw_message: str = ""


class WebhookParserMaster(ABC):
    """Common base for all inbound webhook parsers.

    Subclasses implement :meth:`parse`; the base provides attachment
    persistence and a default signature check.

    Attributes:
        settings (Settings): Shared application configuration.
        log (logging.Logger): Shared application logger.

    Example:
        >>> class Dummy(WebhookParserMaster):
        ...     provider_name = "dummy"
        ...     async def parse(self, request):
        ...         return InboundEmail(provider="dummy")
    """

    def __init__(
        self, settings: Settings, logger: logging.Logger
    ) -> None:
        """Store shared configuration and logger.

        Args:
            settings (Settings): Application configuration snapshot.
            logger (logging.Logger): Shared application logger.

        Returns:
            None
        """
        self.settings = settings
        self.log = logger

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the lowercase provider key this parser handles.

        Returns:
            str: One of ``"sendgrid"``, ``"mailgun"``, ``"elasticemail"``,
                ``"sendcloud"`` or ``"engagelab"``.
        """
        raise NotImplementedError

    @abstractmethod
    async def parse(self, request: Request) -> InboundEmail:
        """Convert a provider payload into a normalised inbound email.

        Implementations read the request body (form or JSON), extract the
        sender / recipient / subject / bodies / spam + auth metadata, read
        any attachment bytes into memory and, for signing providers, set
        :attr:`InboundEmail.signature_verified`.

        Args:
            request (Request): The FastAPI request for the inbound POST.

        Returns:
            InboundEmail: The normalised inbound email.

        Raises:
            WebhookParseError: If the payload is malformed or unreadable.
        """
        raise NotImplementedError

    def verify_signature(self, request: Request) -> bool:
        """Return whether the inbound request is authentic.

        The default implementation trusts the request because SendGrid and
        Elastic Email inbound webhooks are not HMAC-signed (they are secured
        by an unguessable URL / network controls). Mailgun overrides this
        via its parser to verify the ``timestamp``/``token``/``signature``
        triple.

        Args:
            request (Request): The FastAPI request for the inbound POST.

        Returns:
            bool: ``True`` — trusted by default.
        """
        return True

    # ── Shared MIME helpers ──────────────────────────────────────────
    # Every provider that hands us a raw ``.eml`` (EngageLab, SendCloud,
    # SendGrid's header block, Alibaba's IMAP fetch) needs exactly these two
    # walks, so they live here rather than being re-implemented per parser.

    @staticmethod
    def headers_from_mime(msg) -> dict[str, str]:
        """Flatten an :mod:`email.message` object's headers into a dict.

        Keys are lowercased so lookups are case-insensitive — mail servers
        rewrite header casing freely, and this app reads
        ``x-rfq-conversation-id`` back off inbound mail.

        Args:
            msg: An ``email.message.Message`` / ``EmailMessage``.

        Returns:
            dict[str, str]: Lowercase header name → value. On a repeated
                header the last occurrence wins.

        Example:
            >>> import email
            >>> msg = email.message_from_string("Subject: Hi\\n\\nbody")
            >>> WebhookParserMaster.headers_from_mime(msg)["subject"]
            'Hi'
        """
        headers: dict[str, str] = {}
        for key, value in (msg.items() if msg is not None else []):
            headers[str(key).lower()] = str(value)
        return headers

    @staticmethod
    def attachments_from_mime(msg) -> list[RawAttachment]:
        """Extract every attachment part from a parsed MIME message.

        A part counts as an attachment if it is explicitly dispositioned as
        one *or* simply carries a filename — some clients send inline-ish
        parts with a name and no disposition, and dropping those loses real
        files.

        Args:
            msg: An ``email.message.Message`` / ``EmailMessage``.

        Returns:
            list[RawAttachment]: One entry per decodable attachment part;
                empty when the message carried none.
        """
        attachments: list[RawAttachment] = []
        if msg is None:
            return attachments
        for part in msg.walk():
            if (
                part.get_content_disposition() != "attachment"
                and not part.get_filename()
            ):
                continue
            filename = part.get_filename() or "unnamed_attachment"
            content_type = part.get_content_type() or "application/octet-stream"
            # decode=True handles base64 / quoted-printable transparently.
            content = part.get_payload(decode=True)
            if content:
                attachments.append(
                    RawAttachment(filename, content_type, content)
                )
        return attachments

    def persist_attachments(
        self, conv_id: str, attachments: list[RawAttachment]
    ) -> list[dict]:
        """Write attachment bytes to disk and return their metadata.

        Each file is saved under the configured attachments directory with a
        collision-resistant name (``{conv_id}_{batch}_{index}_{filename}``)
        and exposed at the ``{BASE_PATH}/attachments/{name}`` static URL. The per-call
        ``batch`` token (a short random hex) prevents a later reply on the
        same conversation from overwriting an earlier reply's attachment that
        happens to share a filename.

        Args:
            conv_id (str): The conversation the attachments belong to; used
                to namespace the saved filenames.
            attachments (list[RawAttachment]): In-memory attachments from a
                parsed inbound email.

        Returns:
            list[dict]: One metadata dict per saved file with keys
                ``filename``, ``content_type``, ``size`` and ``url``.

        Example:
            >>> meta = parser.persist_attachments(   # doctest: +SKIP
            ...     "3fa9c1b2",
            ...     [RawAttachment("q.pdf", "application/pdf", b"%PDF")])
            >>> meta[0]["url"]                        # doctest: +SKIP
            '/email_poc/attachments/3fa9c1b2_1a2b3c4d_1_q.pdf'
        """
        saved: list[dict] = []
        directory = self.settings.attachments_dir
        directory.mkdir(parents=True, exist_ok=True)

        # One token per inbound message keeps this reply's files distinct
        # from any other reply's files on the same conversation.
        batch = uuid.uuid4().hex[:8]

        for index, att in enumerate(attachments, start=1):
            safe_base = self._safe_filename(att.filename or f"file_{index}")
            safe_name = f"{conv_id}_{batch}_{index}_{safe_base}"
            path = directory / safe_name
            try:
                with open(path, "wb") as handle:
                    handle.write(att.content)
            except OSError as exc:
                # A single bad attachment must not abort the whole reply;
                # log it and keep processing the others.
                self.log.error(
                    "Failed to save attachment %s: %s", safe_name, exc
                )
                continue
            saved.append({
                "filename": att.filename,
                "content_type": att.content_type,
                "size": len(att.content),
                "url": f"{BASE_PATH}/attachments/{safe_name}",
            })
            self.log.debug("Saved attachment %s", safe_name)
        return saved

    def persist_unmatched_attachments(
        self, attachments: list[RawAttachment]
    ) -> list[dict]:
        """Write the files from an unmatched inbound email to disk.

        Same behaviour as :meth:`persist_attachments`, but namespaced
        ``unmatched_{batch}_…`` because there is no conversation to file them
        under yet (requirement 4, step 4). Naming them distinctly also keeps
        them out of the ``{conv_id}_*`` glob that conversation deletion uses.

        Args:
            attachments (list[RawAttachment]): In-memory attachments from a
                parsed inbound email.

        Returns:
            list[dict]: One metadata dict per saved file with keys
                ``filename``, ``content_type``, ``size`` and ``url``.
        """
        return self.persist_attachments("unmatched", attachments)

    @staticmethod
    def _safe_filename(filename: str) -> str:
        """Strip directory components and unsafe characters from a name.

        Prevents path traversal (``../``) and keeps only characters that are
        safe on common filesystems.

        Args:
            filename (str): The sender-supplied filename.

        Returns:
            str: A sanitised basename, never empty.

        Example:
            >>> WebhookParserMaster._safe_filename("../../etc/p w.pdf")
            'p_w.pdf'
        """
        base = filename.replace("\\", "/").split("/")[-1]
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_")
        return cleaned or "attachment"
