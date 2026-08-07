"""FastAPI application factory for EmailPOC.

This module performs all dependency wiring exactly once and exposes the
ready-to-serve :data:`app` object that Uvicorn imports
(``src.app:app``). The construction order is:

1. Load configuration (:func:`src.config.get_settings`).
2. Configure the single shared logger from ``LOG_LEVEL``.
3. Build the async Postgres engine/session factory.
4. Build the default outbound email provider and inbound webhook parser
   (:data:`_INBOUND_EMAIL_PROVIDER`) via their factories. Neither sending
   nor receiving is pinned to that one provider anymore: the sender picks a
   provider per send on the Send RFQ form, inbound arrives at
   ``POST /webhooks/rfq/inbound`` (which works out which provider
   posted), and
   :class:`~src.services.conversation_service.ConversationService` builds
   and caches the rest on demand.
5. Assemble the :class:`ConversationService`.
6. Mount static directories, register templates and include the routes.
7. On startup, launch one background IMAP poller per configured Alibaba
   region (Alibaba has no inbound webhook) — see :func:`lifespan`.

Every collaborator is attached to ``app.state`` so the thin handlers in
:mod:`src.route` can reach them without re-constructing anything.

Example:
    >>> from src.app import app          # doctest: +SKIP
    >>> app.title                        # doctest: +SKIP
    'EmailPOC'
"""

import asyncio
import sys
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.auth.dev_bypass import router as dev_bypass_router
from src.auth.routes import router as auth_router
from src.config import BASE_DIR, BASE_PATH, BEDROCK_BASE_PATH, get_settings
from src.db.repository import Repository
from src.db.session import create_engine_and_sessionmaker
from src.email_platform.factory import EmailProviderFactory
from src.inbound.alibaba_imap_poller import build_alibaba_pollers
from src.logger import AppLogger
from src.route import router
from src.services.conversation_service import ConversationService
from src.webhook_factory.factory import WebhookParserFactory

# bedrock_availability_poc/ is a standalone project (its own README, CLI,
# Dockerfile, standalone `uvicorn app:app`) that imports its own modules
# by bare name (`import config`, `from main import run_check`, ...). Rather
# than fork its code into a package, we put its directory on sys.path so
# those same bare imports resolve correctly when pulled in from here too —
# that keeps it independently runnable exactly as documented, while also
# letting this app mount its routes and serve both POCs from one process
# on one port. Must happen before the `from app import ...` below.
_BEDROCK_DIR = BASE_DIR / "bedrock_availability_poc"
if str(_BEDROCK_DIR) not in sys.path:
    sys.path.insert(0, str(_BEDROCK_DIR))

from app import health as bedrock_health  # noqa: E402
from app import router as bedrock_router  # noqa: E402

# Default provider, used for two things only:
#
# 1. Which parser ``POST /webhooks/rfq/inbound`` falls back to when neither
#    the URL nor the payload names a provider — no provider identifies
#    itself on an inbound POST (see src/route.py::_DEFAULT_INBOUND_PROVIDER
#    and _resolve_inbound_provider).
# 2. Eager credential validation at startup, so a misconfigured default
#    fails fast rather than on the first request.
#
# Every other provider's parser and sender are built on demand — inbound is
# no longer restricted to one payload format (see
# ConversationService.get_parser / get_provider).
_INBOUND_EMAIL_PROVIDER = "engagelab"


def create_app() -> FastAPI:
    """Build, wire and return the FastAPI application.

    Construction is deliberately eager: if :data:`_INBOUND_EMAIL_PROVIDER`
    is missing credentials, the matching factory raises immediately so the
    process fails fast at startup with a clear message rather than on the
    first request. Other providers' credentials are only validated the
    first time a sender selects them (see
    :meth:`~src.services.conversation_service.ConversationService.get_provider`).

    Returns:
        FastAPI: A fully configured application with static mounts,
            templates and routes registered, and all collaborators stored on
            ``app.state``.

    Raises:
        ProviderConfigError: If :data:`_INBOUND_EMAIL_PROVIDER` is unknown
            or missing required configuration.

    Example:
        >>> app = create_app()           # doctest: +SKIP
        >>> "/webhooks/rfq/inbound" in {r.path for r in app.routes}  # noqa
        True
    """
    settings = get_settings()

    # 1. Configure the single shared logger from the env-driven level.
    logger = AppLogger.configure(settings.log_level)
    logger.info(
        "Starting EmailPOC (inbound_provider=%s, log_level=%s)",
        _INBOUND_EMAIL_PROVIDER,
        settings.log_level,
    )

    # 2. Ensure the directories the app reads/writes/serves all exist.
    settings.attachments_dir.mkdir(parents=True, exist_ok=True)
    settings.static_dir.mkdir(parents=True, exist_ok=True)

    # 3. Build infrastructure + the default provider/parser pair.
    engine, session_factory = create_engine_and_sessionmaker(settings.database_url)
    db = Repository(session_factory, settings, logger)
    email_provider = EmailProviderFactory.create(
        _INBOUND_EMAIL_PROVIDER, settings, logger
    )
    webhook_parser = WebhookParserFactory.create(
        _INBOUND_EMAIL_PROVIDER, settings, logger
    )
    service = ConversationService(
        db, email_provider, webhook_parser, settings, logger
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Run the Alibaba IMAP pollers for the lifetime of the app.

        Every other provider pushes inbound mail to
        ``POST /webhooks/rfq/inbound``; Alibaba has no such hook, so
        its replies are pulled by a background task per configured region
        (see src/inbound/alibaba_imap_poller.py). Both feed the same
        ConversationService.process_inbound pipeline.

        NOTE: with more than one replica, every replica polls the same
        mailbox. Set ALIBABA_INBOUND_ENABLED=false on all but one.
        """
        tasks: list[asyncio.Task] = []
        if settings.alibaba_inbound_enabled:
            for poller in build_alibaba_pollers(service, settings, logger):
                tasks.append(asyncio.create_task(poller.run_forever()))
            if not tasks:
                logger.info(
                    "ALIBABA_INBOUND_ENABLED is true but no Alibaba mailbox "
                    "is configured — no IMAP poller started"
                )
        else:
            logger.info("Alibaba IMAP polling disabled by configuration")

        yield

        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()

    # 4. Assemble the FastAPI app and register everything.
    app = FastAPI(title="EmailPOC", lifespan=lifespan)

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        """Log every request across ALL routes (landing page, EmailPOC,
        and the mounted Bedrock POC alike) with the real client IP.

        X-Forwarded-For is a chain: each proxy in the path (e.g. the Hong
        Kong Nginx relay, then the Singapore ALB) appends the IP it saw to
        the end, so the FIRST entry is the original client and later
        entries are each relay hop. One line per request, so the
        ECS/CloudWatch log driver (which ships each stdout line as its own
        event) never shreds a single request's log into multiple entries.

        The ALB hits /health every ~15s from every AZ purely as a liveness
        probe - logging those would drown out real traffic, so they're
        skipped here entirely.
        """
        if request.url.path == "/health":
            return await call_next(request)

        req_id = uuid.uuid4().hex[:8]
        forwarded = request.headers.get("x-forwarded-for")
        client_ip = forwarded.split(",")[0].strip() if forwarded else (
            request.client.host if request.client else None
        )
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        logger.info(
            "req_id=%s client_ip=%s relay_chain=[%s] method=%s path=%s "
            "status=%s duration_ms=%s user_agent=\"%s\"",
            req_id,
            client_ip,
            forwarded or "-",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            request.headers.get("user-agent"),
        )
        return response

    app.mount(
        f"{BASE_PATH}/static",
        StaticFiles(directory=str(settings.static_dir)),
        name="static",
    )
    app.mount(
        f"{BASE_PATH}/attachments",
        StaticFiles(directory=str(settings.attachments_dir)),
        name="attachments",
    )

    # 5. Stash collaborators so the route handlers stay construction-free.
    app.state.settings = settings
    app.state.log = logger
    app.state.db = db
    app.state.email_provider = email_provider
    app.state.webhook_parser = webhook_parser
    app.state.service = service
    app.state.templates = Jinja2Templates(
        directory=str(settings.templates_dir)
    )
    app.state.templates.env.globals["base_path"] = BASE_PATH

    app.include_router(auth_router, prefix=BASE_PATH)
    app.include_router(dev_bypass_router, prefix=BASE_PATH)
    app.include_router(router, prefix=BASE_PATH)

    # 6. Bedrock Availability POC's own routes, mounted into this same app
    # (see the sys.path wiring above) so both POCs are served by one
    # process on one port instead of two separate containers/ports.
    app.include_router(bedrock_router, prefix=BEDROCK_BASE_PATH)
    app.add_api_route("/health", bedrock_health, methods=["GET"])

    # 7. Bare "/" landing page — links out to both POCs, so it's registered
    # on the app directly rather than under BASE_PATH.
    @app.get("/")
    async def landing(request: Request):
        # Prefer an explicit override (e.g. a reverse proxy fronting this
        # app under a different host/path); otherwise derive the link from
        # this very request's own host, so it's correct unmodified on
        # localhost, a LAN IP, or a production domain alike. Same host and
        # port as this landing page itself now that both POCs share one app.
        bedrock_url = settings.bedrock_service_url or (
            f"{request.url.scheme}://{request.url.netloc}{BEDROCK_BASE_PATH}/"
        )
        return app.state.templates.TemplateResponse(request, "landing.html", {
            "base_path": BASE_PATH,
            "bedrock_url": bedrock_url,
            "dev_bypass_enabled": settings.dev_bypass_login,
        })

    logger.info("EmailPOC application ready")
    return app


# The module-level application object Uvicorn imports as ``src.app:app``.
# Defined at import time so ``--reload`` can re-import it on file changes.
app = create_app()
