"""Environment-driven configuration for the EmailPOC application.

This module reads every tunable value from environment variables (loaded
from the project ``.env`` file) and exposes them through a single,
immutable :class:`Settings` object. Centralising configuration here means
no other module has to call :func:`os.getenv` directly, which keeps
provider classes, the webhook parsers and the routes free of scattered
environment lookups.

Configuration is grouped into three concerns:

1. **Global** – ``LOG_LEVEL`` and other app-wide settings.
2. **Per-provider credentials** – the API keys / outbound domain / display
   name each provider needs, e.g. ``ENGAGELAB_API_USER``,
   ``ENGAGELAB_OUTBOUND_DOMAIN``, ``ENGAGELAB_COMPANY_NAME``. There is no
   single "active" provider anymore — the sender picks a provider per send
   on the Send RFQ form's "Provider" dropdown plus a "Supplier Type"
   (Chinese / Non-Chinese); SendCloud and EngageLab both support either
   type, but SendCloud sends through a wholly separate, region-locked
   credential pair for Chinese suppliers (``SENDCLOUD_HK_*``, resolved to
   the internal ``sendcloud_hk`` provider key) vs Non-Chinese
   (``SENDCLOUD_*``), while SendGrid only supports Non-Chinese (see
   ``src.route._SEND_KEYS`` and :func:`src.route.send_email_page`); only
   the credentials for *whichever* resolved provider key is actually used
   need to be configured. :meth:`Settings.provider_outbound_domain` /
   :meth:`Settings.provider_company_name` read them generically by
   provider key so adding a new provider needs no changes here — just its
   ``{PROVIDER}_*`` env vars, a new :class:`~src.email_platform.email_master.EmailMaster`
   subclass and a registration in
   :mod:`src.email_platform.factory`.
3. **Filesystem paths** – computed relative to the repository root so the
   application behaves the same regardless of the current working
   directory.

Example:
    >>> from src.config import get_settings
    >>> settings = get_settings()
    >>> settings.provider_outbound_domain("engagelab")
    'mail.jobsetu.online'
    >>> settings.attachments_dir.name
    'attachments'
"""

import os
import secrets
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# Load ``.env`` exactly once, as early as possible. ``load_dotenv`` is
# idempotent, so importing this module from the Uvicorn reloader subprocess
# (which starts a brand-new interpreter) still picks up the variables.
load_dotenv()

# Repository root = the parent of the ``src`` package directory. Every
# filesystem path below is anchored here so paths never depend on the shell
# working directory the server was launched from.
BASE_DIR = Path(__file__).resolve().parents[1]

# URL prefix the whole app is served under, e.g. http://0.0.0.0:8000/email_poc/.
# Every route, redirect, cookie path, static/attachment mount and template
# link is anchored to this so the app can be moved to a different prefix by
# changing this one constant.
BASE_PATH = "/email_poc"

# Base path the Bedrock Availability POC's routes are mounted under (see
# src/app.py, which pulls in bedrock_availability_poc/app.py's router into
# this same process/port). Must match BASE_PATH in
# bedrock_availability_poc/app.py.
BEDROCK_BASE_PATH = "/check-bedrock"


class Settings:
    """Immutable snapshot of all application configuration.

    A single instance is built once (via :func:`get_settings`) at startup
    and then dependency-injected into the database, the email provider, the
    webhook parser and the service layer. Treat instances as read-only.

    Attributes:
        log_level (str): Logging threshold name, e.g. ``"DEBUG"`` or
            ``"INFO"``. Consumed by :class:`src.logger.AppLogger`.
        sendgrid_api_key (str | None): SendGrid API key.
        mailgun_api_key (str | None): Mailgun private API key.
        mailgun_domain (str | None): Mailgun sending domain.
        mailgun_api_base (str): Mailgun API base URL — ``api.mailgun.net``
            for the US region or ``api.eu.mailgun.net`` for the EU region.
        mailgun_signing_key (str | None): Mailgun HTTP webhook signing key
            used to verify inbound POST authenticity.
        elasticemail_api_key (str | None): Elastic Email API key.
        elasticemail_api_url (str): Elastic Email v4 REST base URL.
        sendcloud_api_user (str | None): SendCloud ``API_USER`` credential.
        sendcloud_api_key (str | None): SendCloud ``API_KEY`` credential.
        sendcloud_api_base (str): SendCloud REST base URL (region-specific).
        sendcloud_hk_outbound_domain (str): Outbound domain for the SendCloud
            Hong Kong/CN region, independent of ``SENDCLOUD_OUTBOUND_DOMAIN``.
        engagelab_api_user (str | None): EngageLab ``API_USER`` credential.
        engagelab_api_key (str | None): EngageLab ``API_KEY`` credential.
        engagelab_api_base (str): EngageLab REST base URL (region-specific).
        base_dir (Path): Repository root directory.
        data_dir (Path): Directory holding the JSON store and attachments.
        attachments_dir (Path): Directory where inbound attachments land.
        static_dir (Path): Directory served at ``/static``.
        templates_dir (Path): Jinja2 templates directory.
        db_path (Path): Path to the JSON store file.

    Example:
        >>> s = Settings()
        >>> s.provider_outbound_domain("engagelab")
        'mail.jobsetu.online'
    """

    def __init__(self) -> None:
        """Read every configuration value from the environment.

        All values are resolved here so the rest of the application can rely
        on plain attribute access. Missing optional values fall back to
        sensible defaults; missing *required* values are validated lazily by
        whichever provider actually needs them (so, for example, you can run
        the SendGrid provider without setting any Mailgun variables).

        Returns:
            None
        """
        # ── Database ──────────────────────────────────────────────────
        self.database_url = os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://postgres:postgres@localhost:5433/emailpoc",
        )

        # ── Auth / sessions ───────────────────────────────────────────
        # Signs the short-lived "pending registration" cookie used between
        # the registration wizard's steps (§3.2) — NOT used for session
        # tokens themselves (those are random + DB-verified, unaffected by
        # this key). Falling back to a fresh random value means existing
        # in-progress registrations are invalidated on every restart; set it
        # explicitly for anything longer-lived than local dev.
        self.secret_key = os.getenv("SECRET_KEY") or secrets.token_urlsafe(32)
        self.session_ttl_days = int(os.getenv("SESSION_TTL_DAYS", "7"))
        self.session_cookie_secure = (
            os.getenv("SESSION_COOKIE_SECURE", "true").strip().lower() == "true"
        )
        # Local-dev-only login bypass (see src/auth/dev_bypass.py). Off by
        # default so a stray deploy never exposes an unauthenticated login.
        self.dev_bypass_login = (
            os.getenv("DEV_BYPASS_LOGIN", "false").strip().lower() == "true"
        )

        # ── Global settings ──────────────────────────────────────────
        self.log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()

        # ── SendGrid credentials ─────────────────────────────────────
        self.sendgrid_api_key = os.getenv("SENDGRID_API_KEY")

        # ── Mailgun credentials ──────────────────────────────────────
        self.mailgun_api_key = os.getenv("MAILGUN_API_KEY")
        self.mailgun_domain = os.getenv("MAILGUN_DOMAIN")
        self.mailgun_api_base = os.getenv(
            "MAILGUN_API_BASE", "https://api.mailgun.net"
        ).rstrip("/")
        self.mailgun_signing_key = os.getenv("MAILGUN_WEBHOOK_SIGNING_KEY")

        # ── Elastic Email credentials ────────────────────────────────
        self.elasticemail_api_key = os.getenv("ELASTICEMAIL_API_KEY")
        self.elasticemail_api_url = os.getenv(
            "ELASTICEMAIL_API_URL", "https://api.elasticemail.com/v4"
        ).rstrip("/")

        # ── SendCloud credentials (Singapore region — non-Chinese suppliers) ──
        self.sendcloud_api_user = os.getenv("SENDCLOUD_API_USER")
        self.sendcloud_api_key = os.getenv("SENDCLOUD_API_KEY")
        # Singapore region (default): https://api.aurorasendcloud.com
        # US region: https://api-us.aurorasendcloud.com
        # CN (Hong Kong SAR) region: https://api-hk.aurorasendcloud.com
        self.sendcloud_api_base = os.getenv(
            "SENDCLOUD_API_BASE", "https://api.aurorasendcloud.com"
        ).rstrip("/")

        # ── SendCloud credentials (Hong Kong/CN region — Chinese suppliers) ──
        # Region-locked: a Singapore apiUser/apiKey pair is rejected against
        # this base URL and vice versa (see
        # setup_docs/aurora_send_cloud/AuroraSendCloud_Documentation.md §2),
        # so this is a wholly separate credential pair from the Singapore one
        # above, used by :class:`~src.email_platform.sendcloud_provider.SendCloudHKEmailProvider`
        # when the sender picks SendCloud for a Chinese supplier.
        self.sendcloud_hk_api_user = os.getenv("SENDCLOUD_HK_API_USER")
        self.sendcloud_hk_api_key = os.getenv("SENDCLOUD_HK_API_KEY")
        self.sendcloud_hk_api_base = os.getenv(
            "SENDCLOUD_HK_API_BASE", "https://api-hk.aurorasendcloud.com"
        ).rstrip("/")
        # Separate outbound domain for the Hong Kong/CN region — independent
        # of ``SENDCLOUD_OUTBOUND_DOMAIN`` (the Singapore region's domain)
        # since Chinese suppliers are sent a From address on whatever domain
        # is verified against the Hong Kong region.
        self.sendcloud_hk_outbound_domain = os.getenv(
            "SENDCLOUD_HK_OUTBOUND_DOMAIN", ""
        )

        # ── EngageLab credentials ────────────────────────────────────
        self.engagelab_api_user = os.getenv("ENGAGELAB_API_USER")
        self.engagelab_api_key = os.getenv("ENGAGELAB_API_KEY")
        # Singapore region (default): https://email.api.engagelab.cc
        # Turkey region: https://emailapi-tr.engagelab.com
        self.engagelab_api_base = os.getenv(
            "ENGAGELAB_API_BASE", "https://email.api.engagelab.cc"
        ).rstrip("/")

        # ── Threading: host part of the Message-IDs this app mints ───
        # Every outbound RFQ carries an app-minted RFC Message-ID
        # (``<rfq.{conv_id}.{uuid}@{this domain}>``) so a reply's
        # In-Reply-To/References can be looked up in our own database. The
        # leading '@' is stripped so both ``imsflow.online`` and
        # ``@imsflow.online`` work. Falls back per provider to that
        # provider's own outbound domain when unset.
        self.message_id_domain = (
            os.getenv("MESSAGE_ID_DOMAIN", "") or ""
        ).strip().lstrip("@")

        # ── Alibaba Enterprise Mail — Singapore (non-Chinese suppliers) ──
        # Alibaba is SMTP-out / IMAP-in rather than an HTTP API, so it needs
        # a mailbox + password instead of an API key pair. The password is
        # the *third-party client security password* generated in the
        # Alibaba admin console — NOT the web login password (see
        # setup_docs/alibaba_guide/Alibaba_Documentation.md).
        #
        # ALIBABA_OUTBOUND_DOMAIN / ALIBABA_COMPANY_NAME are read generically
        # by provider_outbound_domain() / provider_company_name() below, but
        # the domain is also exposed as an attribute here because the
        # provider resolves it directly (it skips the generic base-class
        # constructor to stay symmetrical with its HK subclass).
        self.alibaba_outbound_domain = self.provider_outbound_domain("alibaba")
        # The bare MAIL_* names are the pre-existing keys from the standalone
        # Alibaba scratch script; kept as fallbacks so an existing .env keeps
        # working after this rework.
        self.alibaba_mail_address = os.getenv(
            "ALIBABA_MAIL_ADDRESS"
        ) or os.getenv("MAIL_ADDRESS")
        self.alibaba_mail_password = os.getenv(
            "ALIBABA_MAIL_PASSWORD"
        ) or os.getenv("MAIL_PASSWORD")
        self.alibaba_smtp_host = os.getenv(
            "ALIBABA_SMTP_HOST", "smtp.qiye.aliyun.com"
        )
        self.alibaba_smtp_port = int(os.getenv("ALIBABA_SMTP_PORT", "465"))
        self.alibaba_imap_host = os.getenv(
            "ALIBABA_IMAP_HOST", "imap.qiye.aliyun.com"
        )
        self.alibaba_imap_port = int(os.getenv("ALIBABA_IMAP_PORT", "993"))
        self.alibaba_imap_mailbox = os.getenv("ALIBABA_IMAP_MAILBOX", "INBOX")
        # Optional archive folder; when set, handled messages are moved there
        # instead of merely being flagged \Seen.
        self.alibaba_imap_processed_mailbox = os.getenv(
            "ALIBABA_IMAP_PROCESSED_MAILBOX", ""
        )

        # ── Alibaba Enterprise Mail — Hong Kong (Chinese suppliers) ──────
        # Every value falls back to its Singapore counterpart, so a single
        # mailbox can serve both regions and only the hosts differ. The
        # outbound domain is the exception worth setting explicitly if the
        # two regions send from different verified domains.
        self.alibaba_hk_outbound_domain = (
            os.getenv("ALIBABA_HK_OUTBOUND_DOMAIN", "")
            or self.alibaba_outbound_domain
        )
        self.alibaba_hk_mail_address = (
            os.getenv("ALIBABA_HK_MAIL_ADDRESS") or self.alibaba_mail_address
        )
        self.alibaba_hk_mail_password = (
            os.getenv("ALIBABA_HK_MAIL_PASSWORD") or self.alibaba_mail_password
        )
        self.alibaba_hk_smtp_host = os.getenv(
            "ALIBABA_HK_SMTP_HOST", "smtphk.qiye.aliyun.com"
        )
        self.alibaba_hk_smtp_port = int(
            os.getenv("ALIBABA_HK_SMTP_PORT", str(self.alibaba_smtp_port))
        )
        self.alibaba_hk_imap_host = os.getenv(
            "ALIBABA_HK_IMAP_HOST", "imaphk.qiye.aliyun.com"
        )
        self.alibaba_hk_imap_port = int(
            os.getenv("ALIBABA_HK_IMAP_PORT", str(self.alibaba_imap_port))
        )
        self.alibaba_hk_imap_mailbox = (
            os.getenv("ALIBABA_HK_IMAP_MAILBOX")
            or self.alibaba_imap_mailbox
        )
        self.alibaba_hk_imap_processed_mailbox = (
            os.getenv("ALIBABA_HK_IMAP_PROCESSED_MAILBOX")
            or self.alibaba_imap_processed_mailbox
        )

        # ── Alibaba inbound polling ─────────────────────────────────────
        # Alibaba has no inbound webhook, so replies are polled (see
        # src/inbound/alibaba_imap_poller.py). Set this to false on every
        # replica but one — each replica otherwise polls the same mailbox.
        # Region-specific flags take precedence over the global flag.
        self.alibaba_inbound_enabled = (
            os.getenv("ALIBABA_INBOUND_ENABLED", "true").strip().lower()
            == "true"
        )
        # Singapore region polling (optional; falls back to global flag)
        self.alibaba_imap_polling_enabled = (
            os.getenv("ALIBABA_IMAP_POLLING_ENABLED", "").strip().lower()
        )
        # Hong Kong region polling (optional; falls back to global flag)
        self.alibaba_hk_imap_polling_enabled = (
            os.getenv("ALIBABA_HK_IMAP_POLLING_ENABLED", "").strip().lower()
        )
        self.alibaba_poll_interval_seconds = int(
            os.getenv(
                "ALIBABA_POLL_INTERVAL_SECONDS",
                os.getenv("POLL_INTERVAL_SECONDS", "30"),
            )
        )

        # ── Bedrock Availability POC (linked from the "/" landing page) ──
        # Full override for the "Check Bedrock" button's target URL. Leave
        # unset (the default) to auto-derive it from the incoming request's
        # own host/port + BEDROCK_BASE_PATH — that works unmodified for both
        # local dev and a production host, since the Bedrock routes are
        # mounted on this very app. Set this explicitly only when a reverse
        # proxy fronts this app under a different external host/path than
        # the one it sees the request on.
        self.bedrock_service_url = os.getenv("BEDROCK_SERVICE_URL") or None

        # ── Filesystem paths (anchored at the repository root) ───────
        self.base_dir = BASE_DIR
        self.data_dir = BASE_DIR / "data"
        self.attachments_dir = self.data_dir / "attachments"
        self.static_dir = BASE_DIR / "static"
        self.templates_dir = BASE_DIR / "templates"
        self.db_path = self.data_dir / "db.json"

    # ── Per-provider outbound domain / display name ───────────────────
    # These are read generically by provider key rather than as fixed
    # attributes above, so a brand-new provider only needs its
    # ``{PROVIDER}_OUTBOUND_DOMAIN`` / ``{PROVIDER}_COMPANY_NAME`` env vars
    # (plus a new EmailMaster subclass registered in
    # src/email_platform/factory.py) — nothing here has to change.

    def provider_outbound_domain(self, provider_name: str) -> str:
        """Return the sending domain configured for ``provider_name``.

        Used to build the ``From`` address (see
        :meth:`~src.email_platform.email_master.EmailMaster.build_sending_email`)
        and, when ``MESSAGE_ID_DOMAIN`` is unset, the host part of the
        Message-IDs sent through that provider.

        Every provider has its own domain — ``ENGAGELAB_OUTBOUND_DOMAIN``,
        ``SENDCLOUD_OUTBOUND_DOMAIN``, ``ALIBABA_OUTBOUND_DOMAIN``, … — and
        since this rework they are genuinely independent: inbound is
        per-provider (``POST /webhooks/inbound/{provider}`` plus Alibaba's
        IMAP poller) and matching no longer parses the recipient address at
        all, so there is no reason to keep them equal.

        Args:
            provider_name (str): Provider key, e.g. ``"engagelab"``.

        Returns:
            str: The configured domain, or ``""`` if unset.

        Example:
            >>> Settings().provider_outbound_domain("engagelab")
            'mail.jobsetu.online'
        """
        return os.getenv(f"{provider_name.strip().upper()}_OUTBOUND_DOMAIN", "")

    def provider_company_name(self, provider_name: str) -> str:
        """Return the From-header display name configured for ``provider_name``.

        Args:
            provider_name (str): Provider key, e.g. ``"engagelab"``.

        Returns:
            str: The configured display name, or ``"Your Company"`` if unset.

        Example:
            >>> Settings().provider_company_name("engagelab")
            'JobSetu'
        """
        return os.getenv(
            f"{provider_name.strip().upper()}_COMPANY_NAME", "Your Company"
        )

    @property
    def default_outbound_domain(self) -> str:
        """Best-effort domain for contexts with no provider chosen yet.

        Registration (the permanent ``sending_email`` preview/assignment in
        :mod:`src.auth.routes`) and the dev-bypass "John Carter" shortcut
        (:mod:`src.auth.dev_bypass`) both need *some* domain before any
        provider has been picked on the Send RFQ form. Prefers EngageLab's,
        falling back to SendCloud's — extend this if another provider becomes
        the preferred default.

        Not used for Message-ID hosts: those come from
        :attr:`message_id_domain`, falling back to the *sending* provider's
        own outbound domain (see
        :meth:`~src.email_platform.email_master.EmailMaster.build_message_id`).

        Returns:
            str: The first configured candidate domain, or ``""`` if none
                are set.
        """
        return (
            self.provider_outbound_domain("engagelab")
            or self.provider_outbound_domain("sendcloud")
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` singleton.

    The result is cached with :func:`functools.lru_cache` so the
    environment is parsed only once per process and every caller shares the
    exact same object.

    Returns:
        Settings: The cached configuration snapshot.

    Example:
        >>> from src.config import get_settings
        >>> get_settings() is get_settings()
        True
    """
    return Settings()
