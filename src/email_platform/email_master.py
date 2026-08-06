"""Abstract base class and shared logic for every email provider.

All outbound email providers (SendGrid, Mailgun, Elastic Email, SendCloud,
EngageLab, Alibaba Enterprise Mail) inherit from :class:`EmailMaster`. The
base class owns the behaviour that is identical no matter which provider
actually transmits the message:

- :meth:`EmailMaster.generate_conversation_id` – mint a conversation id.
- :meth:`EmailMaster.build_sending_email` – build the stable per-user
  ``From`` address on this provider's own outbound domain.
- :meth:`EmailMaster.build_rfq_subject` – prefix the subject with
  ``[RFQ - {conv_id}] - ``, and :meth:`EmailMaster.parse_conv_id_from_subject`
  to recover the id from any reply carrying that prefix.
- :meth:`EmailMaster.build_message_id` – mint the RFC 5322 ``Message-ID``
  this app puts on the wire, and :meth:`EmailMaster.parse_message_ids` to
  split an inbound ``In-Reply-To``/``References`` back into its ids.
- :meth:`EmailMaster.build_rfq_html` – render the shared HTML-only RFQ body
  (delegated to :class:`~src.email_platform.rfq_renderer.RfqRenderer`).

**Threading contract.** There is no per-conversation dynamic reply address
anymore. A reply is bound to its conversation by, in order, the RFC
``In-Reply-To``/``References`` headers pointing at a ``Message-ID`` this app
minted, our own ``X-RFQ-Conversation-Id`` header, and finally the
``[RFQ - {conv_id}]`` subject prefix — the one signal that survives every
provider and every mail client. No provider sets a ``Reply-To`` header.

Each instance is constructed with its own :attr:`EmailMaster.outbound_domain`
and :attr:`EmailMaster.company_name`, read generically from
``{PROVIDER}_OUTBOUND_DOMAIN`` / ``{PROVIDER}_COMPANY_NAME`` via
:meth:`~src.config.Settings.provider_outbound_domain` /
:meth:`~src.config.Settings.provider_company_name` — see
:mod:`src.services.conversation_service`, which builds one instance per
selected provider at send time rather than a single app-wide instance.

Each concrete provider only has to implement two members: the
:attr:`EmailMaster.provider_name` property and the
:meth:`EmailMaster.send_email` method, which performs the provider-specific
network call and returns a normalised result dict.

Example:
    >>> from src.email_platform.factory import EmailProviderFactory
    >>> from src.config import get_settings
    >>> from src.logger import AppLogger
    >>> provider = EmailProviderFactory.create(
    ...     "sendgrid", get_settings(), AppLogger.get())
    >>> cid = provider.generate_conversation_id()
    >>> EmailMaster.parse_conv_id_from_subject(
    ...     provider.build_rfq_subject(cid, "Request for Quotation")) == cid
    True
"""

import logging
import re
import uuid
from abc import ABC, abstractmethod
from email.utils import parseaddr

from src.config import Settings
from src.email_platform.rfq_renderer import RfqRenderer

# Recognises the ``[RFQ - HD273HSD]`` subject prefix anywhere in a subject
# line. Module-level so the IMAP poller and the webhook parsers can import it
# without constructing a provider.
#
# Deliberately looser than what :meth:`EmailMaster.build_rfq_subject` writes:
# the token width is 6–16 characters (so the 8-char id can be widened later
# without touching this parser), the separator accepts a plain hyphen or an
# en/em dash, and whitespace is optional — mail clients and Chinese webmail
# alike rewrite subjects more than you would hope. Matching with ``search``
# means ``Re:``/``Fwd:``/``回复:``/``答复:`` prefixes are tolerated for free.
SUBJECT_PREFIX_RE = re.compile(
    r"\[\s*RFQ\s*[-–—]\s*([A-Za-z0-9]{6,16})\s*\]", re.IGNORECASE
)

# Matches one ``<id@host>`` token inside an In-Reply-To / References header.
_MESSAGE_ID_RE = re.compile(r"<[^<>\s]+>")


class EmailProviderError(Exception):
    """Base error for every email-provider failure.

    Catching :class:`EmailProviderError` catches both configuration
    problems (:class:`ProviderConfigError`) and send failures
    (:class:`EmailSendError`).
    """


class ProviderConfigError(EmailProviderError):
    """Raised when a provider is missing required configuration.

    For example, picking ``mailgun`` on the Send RFQ form without setting
    ``MAILGUN_API_KEY`` raises this error with a message naming the missing
    variable.
    """


class EmailSendError(EmailProviderError):
    """Raised when a provider fails to transmit an outbound email.

    Wraps the underlying SDK / HTTP exception in a single, predictable
    error type with a human-readable message so callers do not have to know
    which provider was used.
    """


class EmailMaster(ABC):
    """Common base for all outbound email providers.

    Concrete subclasses implement :attr:`provider_name` and
    :meth:`send_email`; everything else is shared here so the address
    scheme and RFQ template stay identical across providers.

    Attributes:
        settings (Settings): Shared application configuration.
        log (logging.Logger): Shared application logger.

    Example:
        >>> class Dummy(EmailMaster):
        ...     provider_name = "dummy"
        ...     def send_email(self, **kw):
        ...         return {"status_code": 202, "provider": "dummy",
        ...                 "provider_message_id": "x"}
    """

    def __init__(
        self, settings: Settings, logger: logging.Logger
    ) -> None:
        """Store the shared configuration and logger.

        Also resolves this instance's own outbound domain and display name
        from ``{PROVIDER}_OUTBOUND_DOMAIN`` / ``{PROVIDER}_COMPANY_NAME``
        (see :meth:`~src.config.Settings.provider_outbound_domain`), using
        :attr:`provider_name` — safe to read here since subclasses implement
        it as a property that does not depend on any state set later in
        their own ``__init__``.

        Args:
            settings (Settings): The application configuration snapshot.
            logger (logging.Logger): The shared application logger.

        Returns:
            None

        Raises:
            ProviderConfigError: If ``{PROVIDER}_OUTBOUND_DOMAIN`` is not
                set for this provider.
        """
        self.settings = settings
        self.log = logger
        self.outbound_domain = self._require(
            settings.provider_outbound_domain(self.provider_name),
            f"{self.provider_name.upper()}_OUTBOUND_DOMAIN",
        )
        self.company_name = settings.provider_company_name(self.provider_name)

    # ── Provider identity (must be overridden) ───────────────────────

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the lowercase provider key.

        Returns:
            str: One of ``"sendgrid"``, ``"mailgun"``, ``"elasticemail"``,
                ``"sendcloud"`` or ``"engagelab"``.
        """
        raise NotImplementedError

    # ── Shared address helpers ───────────────────────────────────────

    def generate_conversation_id(self) -> str:
        """Generate a short, unique 8-character hex conversation id.

        Uses the first 8 characters of a UUID4 (hyphens removed), giving
        roughly 4 billion unique values — more than enough for this POC.

        Returns:
            str: 8-character lowercase hexadecimal string,
                e.g. ``"3fa9c1b2"``.

        Example:
            >>> cid = provider.generate_conversation_id()
            >>> len(cid)
            8
        """
        return uuid.uuid4().hex[:8]

    def build_sending_email(self, user_name: str) -> str:
        """Construct this provider's stable per-user ``From`` address.

        Converts the user's display name to CamelCase (e.g.
        ``"James Whitfield"`` → ``"JamesWhitfield"``) on this provider's own
        outbound domain. Rebuilt fresh for whichever provider is selected at
        send time, so the domain always matches a domain that provider is
        actually authorised to send from.

        Providers that can only send as one fixed, pre-authenticated mailbox
        override this — see
        :meth:`~src.email_platform.alibaba_provider.AlibabaEnterpriseProvider.build_sending_email`.

        Args:
            user_name (str): The user's display name (e.g. ``"James Whitfield"``).

        Returns:
            str: Fully qualified address, e.g.
                ``"JamesWhitfield@mail.jobsetu.online"``.

        Example:
            >>> provider.build_sending_email("James Whitfield")
            'JamesWhitfield@mail.jobsetu.online'
        """
        camel = "".join(word.capitalize() for word in user_name.split())
        return f"{camel}@{self.outbound_domain}"

    # ── Threading identity: Message-ID + subject token ───────────────

    def build_message_id(self, conv_id: str) -> str:
        """Mint the RFC 5322 ``Message-ID`` this app puts on the wire.

        Minting our own id *before* the send is the only way the inbound
        ``In-Reply-To``/``References`` lookup can hit our own database — a
        provider-generated id is never reported back in a form we could store
        against the outgoing message. Embedding ``conv_id`` in the local part
        also makes the id self-describing when reading raw headers by hand.

        Args:
            conv_id (str): The conversation this message belongs to.

        Returns:
            str: An angle-bracketed id, e.g.
                ``"<rfq.hd273hsd.9f1c….@imsflow.online>"``.

        Example:
            >>> provider.build_message_id("hd273hsd")   # doctest: +SKIP
            '<rfq.hd273hsd.4e1f…@imsflow.online>'
        """
        domain = (
            self.settings.message_id_domain
            or self.outbound_domain
            or "local"
        ).lstrip("@")
        return f"<rfq.{conv_id}.{uuid.uuid4().hex}@{domain}>"

    @staticmethod
    def parse_message_ids(*header_values: str) -> list[str]:
        """Split ``In-Reply-To`` / ``References`` into individual ids.

        ``References`` is ordered oldest→newest, so each header's ids are
        reversed to put the most recent reference first: when a long thread
        offers several candidates, the newest one is the right conversation
        to bind to.

        Args:
            *header_values (str): Raw header values, in priority order (pass
                ``In-Reply-To`` before ``References``).

        Returns:
            list[str]: De-duplicated ``"<id@host>"`` tokens, most recent
                first, preserving the order they were found in.

        Example:
            >>> EmailMaster.parse_message_ids("", "<a@x> <b@x>")
            ['<b@x>', '<a@x>']
        """
        ids: list[str] = []
        for value in header_values:
            ids.extend(reversed(_MESSAGE_ID_RE.findall(value or "")))
        # dict.fromkeys de-dupes while preserving first-seen order.
        return list(dict.fromkeys(ids))

    @staticmethod
    def parse_conv_id_from_subject(subject: str) -> str | None:
        """Extract ``"hd273hsd"`` from any subject carrying the RFQ prefix.

        This is the *guaranteed* fallback in the matching chain: some API
        gateways rewrite ``Message-ID`` and most clients drop custom headers
        on reply, but the subject prefix survives everything short of the
        supplier retyping the subject by hand.

        Args:
            subject (str): The inbound subject line, with or without
                ``Re:``/``Fwd:``/``回复:`` style prefixes.

        Returns:
            str | None: The lowercased conversation id, or ``None`` when the
                subject carries no RFQ prefix.

        Example:
            >>> EmailMaster.parse_conv_id_from_subject(
            ...     "Re: [RFQ - HD273HSD] - Request for Quotation")
            'hd273hsd'
            >>> EmailMaster.parse_conv_id_from_subject("Hello") is None
            True
        """
        match = SUBJECT_PREFIX_RE.search(subject or "")
        return match.group(1).lower() if match else None

    @staticmethod
    def extract_email_address(raw: str) -> str:
        """Strip a display name off a raw ``From``/``To`` header value.

        Inbound payloads often carry ``"James Whitfield <a@b.com>"`` rather
        than a bare address; matching against a stored address (e.g.
        ``users.sending_email``) needs just the ``a@b.com`` part.

        Args:
            raw (str): The raw header value, with or without a display name.

        Returns:
            str: The lowercased bare address, or ``""`` if none could be
                parsed out.

        Example:
            >>> EmailMaster.extract_email_address(
            ...     "James Whitfield <james@mail.jobsetu.online>")
            'james@mail.jobsetu.online'
        """
        return (parseaddr(raw or "")[1] or "").strip().lower()

    # ── Shared RFQ rendering ─────────────────────────────────────────

    def build_rfq_subject(self, conv_id: str, subject_line: str) -> str:
        """Prefix a subject line with the conversation's RFQ token.

        Requirement 2: every outbound RFQ subject is exactly
        ``[RFQ - {conv_id}] - {subject line}``. The token is the whole
        conversation id (not a truncated tag), because
        :meth:`parse_conv_id_from_subject` looks it up directly in
        ``conversations.token`` when a reply arrives with no usable headers.

        Args:
            conv_id (str): The conversation identifier.
            subject_line (str): The human-readable subject, e.g.
                ``"Request for Quotation — Speaker X200"``.

        Returns:
            str: The prefixed subject line.

        Example:
            >>> provider.build_rfq_subject("hd273hsd", "Request for Quotation")
            '[RFQ - hd273hsd] - Request for Quotation'
        """
        return f"[RFQ - {conv_id}] - {subject_line}"

    def build_rfq_html(
        self,
        *,
        conv_id: str,
        supplier_name: str,
        product_name: str,
        quantity,
        target_price: str,
        supplier_type: str = "non_chinese",
    ) -> str:
        """Render the HTML body of an RFQ email.

        Thin delegation to the shared
        :class:`~src.email_platform.rfq_renderer.RfqRenderer` so provider
        code and the service layer keep one call site while the markup itself
        lives in a Jinja template. HTML only — no ``text/plain`` alternative
        is produced anywhere (requirement 8).

        Args:
            conv_id (str): The conversation identifier (shown as the
                reference footer).
            supplier_name (str): Salutation name for the supplier.
            product_name (str): Product being quoted.
            quantity: Number of units requested.
            target_price (str): Buyer's target unit price, e.g. ``"$12.00"``.
            supplier_type (str): ``"chinese"`` selects the Simplified-Chinese
                template; anything else uses the English one.

        Returns:
            str: A complete HTML document ready to use as the email body.

        Example:
            >>> html = provider.build_rfq_html(     # doctest: +SKIP
            ...     conv_id="hd273hsd", supplier_name="Acme",
            ...     product_name="X200", quantity=500,
            ...     target_price="$12.00")
            >>> "Acme" in html                      # doctest: +SKIP
            True
        """
        return self.rfq_renderer.render(
            supplier_type=supplier_type,
            company=self.company_name,
            conv_id=conv_id,
            supplier_name=supplier_name,
            product_name=product_name,
            quantity=quantity,
            target_price=target_price,
        )

    @property
    def rfq_renderer(self) -> RfqRenderer:
        """Return this instance's lazily-built RFQ body renderer.

        Built on first use rather than in ``__init__`` so subclasses that
        deliberately skip ``super().__init__`` (the region-pinned variants —
        see :class:`~src.email_platform.sendcloud_provider.SendCloudHKEmailProvider`)
        do not each have to remember to construct one.

        Returns:
            RfqRenderer: The cached renderer for this provider instance.
        """
        if getattr(self, "_rfq_renderer", None) is None:
            self._rfq_renderer = RfqRenderer(self.settings)
        return self._rfq_renderer

    # ── Provider-specific transmission (must be overridden) ──────────

    @abstractmethod
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
        """Transmit a single email through the concrete provider.

        Implementations perform the provider-specific network call and
        normalise the result so callers never depend on a provider's native
        response shape.

        Every implementation must put ``message_id`` on the wire as the RFC
        ``Message-ID`` header (SMTP sets it directly; API providers pass it
        through their custom-header field) and must send an HTML body only —
        no ``text/plain`` alternative. **No implementation sets a
        ``Reply-To`` header**: replies are threaded by ``Message-ID`` and the
        subject token, not by a per-conversation address.

        Args:
            from_email (str): Verified sender address for the ``From``
                header.
            from_name (str): Display name for the ``From`` header.
            to_email (str): Recipient address.
            to_name (str): Recipient display name.
            subject (str): Subject line, already carrying the
                ``[RFQ - {conv_id}]`` prefix.
            html_body (str): HTML body of the message.
            message_id (str): The RFC 5322 ``Message-ID`` this app minted for
                the message (see :meth:`build_message_id`).
            extra_headers (dict[str, str] | None): Additional headers to set,
                e.g. ``{"X-RFQ-Conversation-Id": conv_id}``.
            attachments (list | None): Optional list of attachment dicts,
                each with keys ``filename`` (str), ``content`` (bytes) and
                ``content_type`` (str).

        Returns:
            dict: Normalised result with keys ``status_code`` (int),
                ``provider`` (str), ``provider_message_id`` (str | None) and
                ``message_id`` (str) — the last echoing back what actually
                went out, so the caller persists the real header value.

        Raises:
            ProviderConfigError: If required credentials are missing.
            EmailSendError: If the provider rejects or fails the send.
        """
        raise NotImplementedError

    # ── Internal helpers shared by subclasses ────────────────────────

    def _require(self, value: str | None, name: str) -> str:
        """Return ``value`` or raise if it is missing/empty.

        Subclasses call this to validate that a required credential or
        setting is present before attempting a send.

        Args:
            value (str | None): The configuration value to check.
            name (str): The environment-variable name, used in the error
                message so operators know exactly what to fix.

        Returns:
            str: The validated, non-empty value.

        Raises:
            ProviderConfigError: If ``value`` is ``None`` or empty.

        Example:
            >>> provider._require("abc", "SENDGRID_API_KEY")
            'abc'
        """
        if not value:
            raise ProviderConfigError(
                f"Missing required configuration: {name}. "
                f"Set it in your .env file."
            )
        return value
