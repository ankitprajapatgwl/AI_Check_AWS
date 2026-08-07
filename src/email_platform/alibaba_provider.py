"""Outbound email provider backed by Alibaba Enterprise Mail (阿里云企业邮箱).

Unlike every other provider in this package, Alibaba has no REST send API —
:class:`AlibabaEnterpriseProvider` speaks plain SMTP over implicit SSL
(port 465) with the stdlib :mod:`smtplib`, and inbound replies arrive by
IMAP polling rather than a webhook (see
:mod:`src.inbound.alibaba_imap_poller`).

Two consequences worth knowing before reading the code:

1. **One authenticated mailbox.** Alibaba's SMTP server only accepts a
   ``From`` that is the authenticated account (or one of its registered
   aliases), so :meth:`AlibabaEnterpriseProvider.build_sending_email`
   overrides the per-user address scheme and always returns
   ``ALIBABA_MAIL_ADDRESS``. The sending user's identity is carried in the
   ``From`` **display name** instead. If per-user addresses are ever needed,
   register them as mailbox aliases in the Alibaba admin console first.
2. **Blocking I/O.** An SMTP round trip is slow and synchronous, so callers
   must run :meth:`AlibabaEnterpriseProvider.send_email` off the event loop
   (:func:`asyncio.to_thread`) — which is what
   :meth:`~src.services.conversation_service.ConversationService.send_rfq`
   does for every provider.

Two regions are registered, but **only Hong Kong is used for sending**:

================================ ========================================
Class / factory key              Region
================================ ========================================
``AlibabaEnterpriseProvider``    Singapore — ``smtp.qiye.aliyun.com``
(``"alibaba"``)                  (not routed to; base class for the below)
``AlibabaHKEnterpriseProvider``  Hong Kong — ``smtphk.qiye.aliyun.com``
(``"alibaba_hk"``)               (**both** supplier types)
================================ ========================================

``src.route._SEND_KEYS`` resolves Alibaba + *either* supplier type to
``"alibaba_hk"``: one endpoint, one mailbox, one SMTP route, one place for
replies to land. The Singapore class stays registered because the Hong Kong
one subclasses it, historical conversations recorded ``send_key='alibaba'``,
and re-splitting the two is a one-line change in that table.

Configuration consumed (see :class:`src.config.Settings` and
``setup_docs/alibaba_guide/Alibaba_Documentation.md``):

- ``ALIBABA_MAIL_ADDRESS`` *(required)* – the authenticated mailbox.
- ``ALIBABA_MAIL_PASSWORD`` *(required)* – the **third-party client security
  password** generated in the Alibaba admin console, *not* the web login
  password.
- ``ALIBABA_OUTBOUND_DOMAIN`` *(required)* – this provider's own sending
  domain, kept separate from every other provider's so Alibaba can be
  pointed at a different domain than SendCloud/EngageLab.
- ``ALIBABA_SMTP_HOST`` / ``ALIBABA_SMTP_PORT`` – region host and port.
- ``ALIBABA_COMPANY_NAME`` – display name used in the RFQ body.

The Hong Kong variant reads the same names prefixed ``ALIBABA_HK_*``, each
falling back to the Singapore value when unset (see
:class:`src.config.Settings`), so a single mailbox can serve both regions.

Example:
    >>> from src.email_platform.alibaba_provider import (
    ...     AlibabaEnterpriseProvider)
    >>> from src.config import get_settings
    >>> from src.logger import AppLogger
    >>> provider = AlibabaEnterpriseProvider(      # doctest: +SKIP
    ...     get_settings(), AppLogger.get())
    >>> provider.provider_name                     # doctest: +SKIP
    'alibaba'
"""

import logging
import smtplib
from email import encoders
from email.header import Header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

from src.config import Settings
from src.email_platform.email_master import EmailMaster, EmailSendError

# SMTP connect + command timeout in seconds. Generous because the Singapore
# and Hong Kong endpoints are both remote from most deployments.
_SMTP_TIMEOUT = 30

# SMTP's own "action completed" reply code. There is no HTTP status here, so
# this is what a successful send reports back to keep the result dict's shape
# identical to the API-backed providers'.
_SMTP_OK = 250


class AlibabaEnterpriseProvider(EmailMaster):
    """Send RFQ emails through Alibaba Enterprise Mail over SMTP+SSL.

    Singapore region. Nothing routes here anymore — every Alibaba send goes
    through :class:`AlibabaHKEnterpriseProvider`, which subclasses this and
    pins Hong Kong. Sends from the single authenticated mailbox
    (``ALIBABA_MAIL_ADDRESS``); the per-user identity lives in the display
    name, since Alibaba SMTP rejects a ``From`` that is not the
    authenticated account.

    Attributes:
        settings (Settings): Shared application configuration.
        log (logging.Logger): Shared application logger.
        mail_address (str): The authenticated mailbox, used as ``From``.
        mail_password (str): The third-party client security password.
        smtp_host (str): Region SMTP host.
        smtp_port (int): Region SMTP port (465 = implicit SSL).

    Example:
        >>> provider = AlibabaEnterpriseProvider(settings, logger)
        >>> provider.provider_name                 # doctest: +SKIP
        'alibaba'
    """

    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        """Validate the mailbox credentials and resolve the region hosts.

        Deliberately does not call :meth:`EmailMaster.__init__`: the base
        class resolves ``outbound_domain`` generically from
        ``{PROVIDER}_OUTBOUND_DOMAIN``, which this class does too — but the
        subclass below needs to substitute its own region settings, and
        keeping both constructors symmetrical (each resolving its own four
        values) makes the difference between them obvious.

        Args:
            settings (Settings): Shared application configuration.
            logger (logging.Logger): Shared application logger.

        Returns:
            None

        Raises:
            ProviderConfigError: If ``ALIBABA_OUTBOUND_DOMAIN``,
                ``ALIBABA_MAIL_ADDRESS`` or ``ALIBABA_MAIL_PASSWORD`` is not
                set.
        """
        self.settings = settings
        self.log = logger
        self.outbound_domain = self._require(
            settings.alibaba_outbound_domain, "ALIBABA_OUTBOUND_DOMAIN"
        )
        self.company_name = settings.provider_company_name(self.provider_name)
        self.mail_address = self._require(
            settings.alibaba_mail_address, "ALIBABA_MAIL_ADDRESS"
        )
        self.mail_password = self._require(
            settings.alibaba_mail_password, "ALIBABA_MAIL_PASSWORD"
        )
        self.smtp_host = settings.alibaba_smtp_host
        self.smtp_port = settings.alibaba_smtp_port
        self.log.info(
            "Alibaba Enterprise Mail provider initialised (smtp=%s:%s, from=%s)",
            self.smtp_host,
            self.smtp_port,
            self.mail_address,
        )

    @property
    def provider_name(self) -> str:
        """Return the provider key ``"alibaba"``.

        Both regions report the same name — as with SendCloud, only the
        region's hosts and credentials differ, so configuration lookups, the
        company name and the conversation's stored ``provider`` read the same
        either way. The *resolved* region is recorded separately in
        ``conversations.send_key``.

        Returns:
            str: Always ``"alibaba"``.
        """
        return "alibaba"

    def build_sending_email(self, user_name: str) -> str:
        """Return the single authenticated mailbox, ignoring ``user_name``.

        Overrides the base class's ``{CamelName}@{domain}`` scheme: Alibaba
        SMTP rejects any ``From`` that is not the authenticated account, so
        every user sends from the same address and is distinguished only by
        the ``From`` display name.

        Args:
            user_name (str): The sending user's display name — unused here,
                but part of the shared signature.

        Returns:
            str: ``ALIBABA_MAIL_ADDRESS``.

        Example:
            >>> provider.build_sending_email("James Whitfield")  # noqa
            'ankit.prajapat@imsflow.online'
        """
        return self.mail_address

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
        """Send one email over Alibaba's SMTP+SSL endpoint.

        Because this is real SMTP rather than an API, the ``Message-ID``
        header is set directly on the MIME message — no provider can rewrite
        it, which makes Alibaba the most reliable of the providers for
        header-based threading.

        Args:
            from_email (str): Ignored in favour of :attr:`mail_address` —
                Alibaba only permits the authenticated account as ``From``.
                Accepted for interface parity, and logged when it differs.
            from_name (str): Display name for the ``From`` header; this is
                where the sending user's identity actually appears.
            to_email (str): Recipient address.
            to_name (str): Recipient display name.
            subject (str): Subject line (RFC 2047 encoded as UTF-8, so the
                Chinese templates' subjects survive intact).
            html_body (str): HTML body — attached as the only body part.
            message_id (str): The RFC ``Message-ID`` this app minted.
            extra_headers (dict[str, str] | None): Extra headers to set,
                e.g. ``X-RFQ-Conversation-Id``.
            attachments (list | None): Optional attachment dicts with
                ``filename`` / ``content`` / ``content_type`` keys.

        Returns:
            dict: ``{"status_code": 250, "provider": "alibaba",
                "provider_message_id": str, "message_id": str}``. Over SMTP
                there is no separate provider id, so our own ``Message-ID``
                *is* the provider id.

        Raises:
            EmailSendError: If the connection, login or send fails.

        Example:
            >>> provider.send_email(                    # doctest: +SKIP
            ...     from_email="ankit.prajapat@imsflow.online",
            ...     from_name="James Whitfield", to_email="buyer@x.com",
            ...     to_name="Buyer", subject="[RFQ - hd273hsd] - Hi",
            ...     html_body="<p>Hi</p>",
            ...     message_id="<rfq.hd273hsd.ab@imsflow.online>")
            {'status_code': 250, 'provider': 'alibaba', ...}
        """
        if from_email and from_email != self.mail_address:
            self.log.debug(
                "Alibaba ignores From %s; sending as the authenticated "
                "mailbox %s with display name %r",
                from_email,
                self.mail_address,
                from_name,
            )

        # "mixed" is the right multipart subtype whenever attachment parts
        # sit alongside the body; with no attachments the default "mixed"
        # container still holds a single text/html part just fine — and
        # never a text/plain alternative (requirement 8).
        msg = MIMEMultipart("mixed")
        msg["Message-ID"] = message_id
        msg["From"] = formataddr(
            (str(Header(from_name or self.company_name, "utf-8")), self.mail_address)
        )
        msg["To"] = (
            formataddr((str(Header(to_name, "utf-8")), to_email))
            if to_name
            else to_email
        )
        msg["Subject"] = Header(subject, "utf-8")
        msg["Date"] = formatdate(localtime=True)
        for key, value in (extra_headers or {}).items():
            msg[key] = value

        msg.attach(MIMEText(html_body, "html", "utf-8"))

        for att in attachments or []:
            # MIMEApplication would flatten everything to
            # application/octet-stream; MIMEBase preserves the real type, so
            # a PDF arrives as a PDF and an image previews inline-ish in the
            # recipient's client.
            content_type = att.get("content_type") or "application/octet-stream"
            maintype, _, subtype = content_type.partition("/")
            part = MIMEBase(maintype or "application", subtype or "octet-stream")
            part.set_payload(att["content"])
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition", "attachment", filename=att["filename"]
            )
            msg.attach(part)

        try:
            with smtplib.SMTP_SSL(
                self.smtp_host, self.smtp_port, timeout=_SMTP_TIMEOUT
            ) as client:
                client.login(self.mail_address, self.mail_password)
                client.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            self.log.error("Alibaba SMTP send failed: %s", exc)
            raise EmailSendError(
                f"Alibaba SMTP failed sending to {to_email}: {exc}"
            ) from exc

        self.log.info(
            "Alibaba accepted email to %s (message_id=%s)", to_email, message_id
        )
        return {
            "status_code": _SMTP_OK,
            "provider": self.provider_name,
            # SMTP has no separate provider-side id: what we minted is what
            # was delivered, so it serves as both.
            "provider_message_id": message_id,
            "message_id": message_id,
        }


class AlibabaHKEnterpriseProvider(AlibabaEnterpriseProvider):
    """Alibaba Enterprise Mail, pinned to the Hong Kong region.

    Registered under the separate factory key ``"alibaba_hk"`` (see
    :mod:`src.email_platform.factory`) and selected by :mod:`src.route`
    whenever the sender picks Alibaba — for **either** supplier type. Hong
    Kong's ``smtphk.qiye.aliyun.com`` sits closer to Chinese mailboxes than
    the Singapore endpoint and serves the rest just as well, so routing
    everything through it keeps one mailbox and one reply path.
    Everything about sending is identical to
    :class:`AlibabaEnterpriseProvider`; only the hosts, credentials and
    outbound domain differ, and each ``ALIBABA_HK_*`` value falls back to its
    Singapore counterpart so one mailbox can serve both regions.

    :attr:`provider_name` intentionally stays ``"alibaba"`` (inherited, not
    overridden) so the company name and the conversation's stored
    ``provider`` field read the same regardless of which region sent — the
    resolved region is recorded in ``conversations.send_key``.

    Example:
        >>> provider = AlibabaHKEnterpriseProvider(settings, logger)
        >>> provider.provider_name                     # doctest: +SKIP
        'alibaba'
    """

    def __init__(self, settings: Settings, logger: logging.Logger) -> None:
        """Resolve the Hong Kong region's hosts, credentials and domain.

        Deliberately skips :meth:`AlibabaEnterpriseProvider.__init__`, which
        would read the Singapore ``ALIBABA_*`` values via the shared
        ``"alibaba"`` :attr:`provider_name`.

        Args:
            settings (Settings): Shared application configuration.
            logger (logging.Logger): Shared application logger.

        Returns:
            None

        Raises:
            ProviderConfigError: If ``ALIBABA_HK_OUTBOUND_DOMAIN`` (or the
                mailbox credentials, after their Singapore fallback) is not
                set.
        """
        self.settings = settings
        self.log = logger
        self.outbound_domain = self._require(
            settings.alibaba_hk_outbound_domain, "ALIBABA_HK_OUTBOUND_DOMAIN"
        )
        self.company_name = settings.provider_company_name(self.provider_name)
        self.mail_address = self._require(
            settings.alibaba_hk_mail_address, "ALIBABA_HK_MAIL_ADDRESS"
        )
        self.mail_password = self._require(
            settings.alibaba_hk_mail_password, "ALIBABA_HK_MAIL_PASSWORD"
        )
        self.smtp_host = settings.alibaba_hk_smtp_host
        self.smtp_port = settings.alibaba_hk_smtp_port
        self.log.info(
            "Alibaba Enterprise Mail (Hong Kong region) provider initialised "
            "(smtp=%s:%s, from=%s)",
            self.smtp_host,
            self.smtp_port,
            self.mail_address,
        )
