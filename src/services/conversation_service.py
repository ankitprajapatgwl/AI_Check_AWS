"""Conversation orchestration for EmailPOC.

:class:`ConversationService` is the single place that coordinates the
database, outbound email providers and the inbound parsers. It exposes four
high-level operations:

1. :meth:`ConversationService.create_draft` – mint a ``conv_id`` and persist
   a ``status='draft'`` conversation *before* anything is sent.
2. :meth:`ConversationService.send_rfq` – mint the RFC ``Message-ID``, send
   the RFQ through the selected provider, persist the sent email and flip the
   conversation ``draft → open``.
3. :meth:`ConversationService.handle_inbound` – parse one provider's inbound
   webhook request and hand it to the pipeline below.
4. :meth:`ConversationService.process_inbound` – the provider-agnostic
   matching pipeline, shared by every webhook *and* by the Alibaba IMAP
   poller.

**Conversation lifecycle**::

    create_draft()            send_rfq()            reply matched
    ────────────►  draft  ──────────────►  open  ──────────────►  closed
                            │
                            └── provider rejected ──►  failed

**Inbound matching order** (requirement 4). Each step covers a gap the
previous one cannot:

1. ``In-Reply-To`` / ``References`` → ``emails.message_id``. The correct
   answer whenever the supplier used their client's Reply button.
2. ``X-RFQ-Conversation-Id`` → ``conversations.token``. Cheap and exact, but
   most clients drop custom headers on reply, so it rarely fires.
3. ``[RFQ - {conv_id}]`` subject prefix → ``conversations.token``. The
   guaranteed fallback: it survives header-stripping gateways and manual
   forwards alike.
4. No match → the whole email (bodies, headers, attachments) is persisted to
   ``unmatched_emails`` rather than dropped.

There is no dynamic per-conversation reply address anywhere in this flow.

Provider selection is per send: the Send RFQ form has a "Provider" dropdown
plus a "Supplier Type" (Chinese / Non-Chinese), and the route layer resolves
that pair to a factory key — SendCloud + Chinese → ``sendcloud_hk``, while
Alibaba resolves to ``alibaba_hk`` for *both* supplier types (its Hong Kong
server sends everything) — see ``src.route._SEND_KEYS``.
:meth:`ConversationService.get_provider` and
:meth:`ConversationService.get_parser` build and cache an instance per key on
demand, so adding a provider needs no changes here.

Example:
    >>> service = ConversationService(           # doctest: +SKIP
    ...     db, email_provider, webhook_parser, settings, logger)
    >>> draft = await service.create_draft(       # doctest: +SKIP
    ...     user_id="42", supplier_email="supplier@acme.com",
    ...     supplier_name="Acme", supplier_type="non_chinese",
    ...     product_name="Speaker X200")
    >>> draft["status"]                           # doctest: +SKIP
    'draft'
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request

from src.config import Settings
from src.db.repository import DuplicateConversationTokenError, Repository
from src.email_platform.email_master import (
    EmailMaster,
    EmailProviderError,
    ProviderConfigError,
)
from src.email_platform.factory import EmailProviderFactory
from src.webhook_factory.factory import WebhookParserFactory
from src.webhook_factory.webhook_master import (
    InboundEmail,
    WebhookParseError,
    WebhookParserMaster,
)

# Inbound emails scoring above this SpamAssassin-style threshold are
# discarded before being matched to a conversation.
_SPAM_THRESHOLD = 5.0

# Bounded retry for the rare case a freshly generated 8-char conversation
# token collides with an existing one (the DB's UNIQUE constraint is the
# real backstop; this just turns a collision into a silent re-pick instead
# of a user-facing error).
_MAX_TOKEN_ATTEMPTS = 5

# Custom header carrying the conversation id on every outbound RFQ. Read back
# on inbound as matching strategy 2 — only useful when the replying client
# preserved it, which most do not, hence the subject-prefix fallback.
_CONVERSATION_ID_HEADER = "X-RFQ-Conversation-Id"


class ConversationService:
    """Coordinate conversations, outbound sends and inbound replies.

    Attributes:
        db (Repository): The async Postgres persistence layer.
        email (EmailMaster): The default outbound provider — used only for
            its shared, provider-independent helpers when no specific
            provider has been selected.
        webhook (WebhookParserMaster): The default inbound parser, kept as
            the parser for the legacy un-suffixed webhook path and used for
            attachment persistence (which is provider-independent).
        settings (Settings): Shared application configuration.
        log (logging.Logger): Shared application logger.

    Example:
        >>> service = ConversationService(       # doctest: +SKIP
        ...     db, email_provider, webhook_parser, settings, logger)
    """

    def __init__(
        self,
        db: Repository,
        email_provider: EmailMaster,
        webhook_parser: WebhookParserMaster,
        settings: Settings,
        logger: logging.Logger,
    ) -> None:
        """Store the collaborators this service orchestrates.

        Args:
            db (Repository): The async Postgres persistence layer.
            email_provider (EmailMaster): The default outbound provider.
            webhook_parser (WebhookParserMaster): The default inbound parser.
            settings (Settings): Shared application configuration.
            logger (logging.Logger): Shared application logger.

        Returns:
            None
        """
        self.db = db
        self.email = email_provider
        self.webhook = webhook_parser
        self.settings = settings
        self.log = logger
        self._provider_cache: dict[str, EmailMaster] = {
            email_provider.provider_name: email_provider
        }
        self._parser_cache: dict[str, WebhookParserMaster] = {
            webhook_parser.provider_name: webhook_parser
        }

    def get_provider(self, provider_name: str) -> EmailMaster:
        """Return the :class:`EmailMaster` instance for ``provider_name``.

        Instances are built lazily (via
        :class:`~src.email_platform.factory.EmailProviderFactory`, which
        validates that provider's credentials) and cached, so selecting the
        same provider on a later send reuses the same instance instead of
        re-validating configuration every time.

        Args:
            provider_name (str): Resolved factory key, e.g. ``"alibaba_hk"``.

        Returns:
            EmailMaster: The (possibly newly built) provider instance.

        Raises:
            ProviderConfigError: If ``provider_name`` is empty, unknown, or
                that provider is missing required configuration.
        """
        key = (provider_name or "").strip().lower()
        if not key:
            raise ProviderConfigError("No email provider selected.")
        if key not in self._provider_cache:
            self._provider_cache[key] = EmailProviderFactory.create(
                key, self.settings, self.log
            )
        return self._provider_cache[key]

    def get_parser(self, provider_name: str) -> WebhookParserMaster:
        """Return the inbound parser for ``provider_name``.

        Mirrors :meth:`get_provider` on the inbound side. Inbound has to work
        for every provider, so the parser is resolved per request from the
        URL rather than pinned once at startup.

        Args:
            provider_name (str): Provider key from the webhook URL.

        Returns:
            WebhookParserMaster: The (possibly newly built) parser instance.

        Raises:
            ProviderConfigError: If ``provider_name`` is empty or unknown.
        """
        key = (provider_name or "").strip().lower()
        if not key:
            raise ProviderConfigError("No inbound provider specified.")
        if key not in self._parser_cache:
            self._parser_cache[key] = WebhookParserFactory.create(
                key, self.settings, self.log
            )
        return self._parser_cache[key]

    # ── Outbound ─────────────────────────────────────────────────────

    async def create_draft(
        self,
        *,
        user_id: str,
        supplier_email: str,
        supplier_name: str = "",
        supplier_type: str = "",
        product_name: str = "",
        quantity=None,
        target_price: str = "",
        project_id: str = "",
        project_name: str = "",
        provider_name: str = "",
        send_key: str = "",
        subject_line: str = "",
    ) -> dict:
        """Mint a conversation id and persist it as a draft (requirement 1).

        Nothing is sent here. The row exists — with its final, prefixed
        subject already computed (requirement 2) — so the form can show the
        real reference before sending, and so a failed send still leaves a
        record of what was attempted.

        Retries with a freshly generated token, up to
        :data:`_MAX_TOKEN_ATTEMPTS` times, if it collides with an existing
        conversation's; the DB's UNIQUE constraint on ``conversations.token``
        is what actually guarantees no duplicates.

        Args:
            user_id (str): The platform user UUID who owns this conversation.
            supplier_email (str): The supplier address for the outbound RFQ.
            supplier_name (str): Human-readable supplier display name.
            supplier_type (str): ``"chinese"`` or ``"non_chinese"`` — decides
                the body template's language and the sending region.
            product_name (str): Product being quoted.
            quantity: Units requested.
            target_price (str): Target unit price, e.g. ``"$12.00"``.
            project_id (str): UUID of the selected catalog product, if any.
            project_name (str): That product's name.
            provider_name (str): The user-facing provider key, e.g.
                ``"alibaba"``. May be empty at draft time.
            send_key (str): The resolved factory key, e.g. ``"alibaba_hk"``.
            subject_line (str): Override for the un-prefixed subject; the
                default is language-appropriate for ``supplier_type``.

        Returns:
            dict: The stored conversation, including ``conv_id`` and the
                final prefixed ``subject``.

        Raises:
            DuplicateConversationTokenError: If every attempt collides
                (astronomically unlikely with an 8-char hex token space).
        """
        provider = self.email
        subject_line = subject_line or self._default_subject_line(
            product_name, supplier_type
        )
        now = datetime.now(timezone.utc).isoformat()
        last_error: DuplicateConversationTokenError | None = None

        for _ in range(_MAX_TOKEN_ATTEMPTS):
            conv_id = provider.generate_conversation_id()
            conversation = {
                "conv_id": conv_id,
                "thread_id": conv_id,
                "project_id": project_id,
                "project_name": project_name,
                "user_id": str(user_id),
                "supplier_email": supplier_email,
                "supplier_name": supplier_name,
                "supplier_type": supplier_type,
                "product_name": product_name,
                "quantity": quantity,
                "target_price": target_price,
                "provider": provider_name,
                "send_key": send_key,
                "subject": provider.build_rfq_subject(conv_id, subject_line),
                "status": "draft",
                "created_at": now,
                "reply_count": 0,
                "last_reply_at": None,
                "emails_sent": [],
                "emails_received": [],
            }
            try:
                await self.db.insert_conversation(conversation)
            except DuplicateConversationTokenError as exc:
                last_error = exc
                self.log.warning(
                    "Conversation token collision on %s, retrying", conv_id
                )
                continue

            self.log.info(
                "Created draft conversation %s (user=%s provider=%s type=%s)",
                conv_id,
                user_id,
                provider_name or "unset",
                supplier_type or "unset",
            )
            return conversation

        raise last_error

    async def send_rfq(
        self,
        *,
        conv_id: str,
        user_id: str,
        provider_name: str,
        attachments: list | None = None,
        subject_line: str | None = None,
    ) -> dict:
        """Send a draft conversation's RFQ, then record what went out.

        The RFC ``Message-ID`` is minted **before** the send and handed to the
        provider, because it is the only way a later reply's ``In-Reply-To``
        can be looked up against this app's own database. On success the
        conversation flips ``draft → open`` and the sent email row captures
        the real ``message_id``, the provider's own id, the status code, the
        subject and the timestamps (requirement 3). On a provider failure the
        conversation is marked ``failed`` and the error is re-raised for the
        route to surface.

        Args:
            conv_id (str): The draft conversation to send.
            user_id (str): The user sending (used for the From display name).
            provider_name (str): The resolved factory key to send through,
                e.g. ``"alibaba_hk"``.
            attachments (list | None): Attachment dicts with ``filename`` /
                ``content`` / ``content_type``.
            subject_line (str | None): Only used if the draft somehow has no
                stored subject.

        Returns:
            dict: ``{"status_code", "provider", "from", "to", "conv_id",
                "subject", "message_id"}``.

        Raises:
            EmailProviderError: If the provider is misconfigured or the send
                fails.
            ValueError: If ``conv_id`` does not exist.
        """
        conversation = await self.db.get_conversation(conv_id)
        if not conversation:
            raise ValueError(f"Unknown conversation: {conv_id}")

        provider = self.get_provider(provider_name)
        user = await self.db.get_user_auth_by_id(user_id)
        user_name = (user or {}).get("full_name") or str(user_id)

        from_email = provider.build_sending_email(user_name)
        subject = conversation.get("subject") or provider.build_rfq_subject(
            conv_id,
            subject_line
            or self._default_subject_line(
                conversation.get("product_name") or "",
                conversation.get("supplier_type") or "",
            ),
        )
        message_id = provider.build_message_id(conv_id)
        html_body = provider.build_rfq_html(
            conv_id=conv_id,
            supplier_name=conversation.get("supplier_name") or "",
            product_name=conversation.get("product_name") or "",
            quantity=conversation.get("quantity") or "",
            target_price=conversation.get("target_price") or "",
            supplier_type=conversation.get("supplier_type") or "",
        )

        try:
            # Every provider's send is blocking I/O — an HTTP round trip for
            # the API-backed ones, a full SMTP conversation for Alibaba — so
            # none of them may run on the event loop.
            result = await asyncio.to_thread(
                provider.send_email,
                from_email=from_email,
                from_name=user_name or provider.company_name,
                to_email=conversation["supplier_email"],
                to_name=conversation.get("supplier_name") or "",
                subject=subject,
                html_body=html_body,
                message_id=message_id,
                extra_headers={_CONVERSATION_ID_HEADER: conv_id},
                attachments=attachments,
            )
        except EmailProviderError:
            await self.db.mark_conversation_failed(conv_id)
            raise

        sent_message_id = result.get("message_id") or message_id
        await self.db.add_sent_email(
            conv_id,
            {
                "message_id": sent_message_id,
                "provider_message_id": result.get("provider_message_id"),
                "status_code": result.get("status_code"),
                "from_email": from_email,
                "to_email": conversation["supplier_email"],
                "subject": subject,
                "body_html": html_body,
                "email_type": "new_thread",
                "provider": result.get("provider"),
                "attachments": [
                    {
                        "filename": a["filename"],
                        "content_type": a.get(
                            "content_type", "application/octet-stream"
                        ),
                        "size": len(a["content"]),
                    }
                    for a in (attachments or [])
                ],
            },
        )
        await self.db.mark_conversation_sent(
            conv_id,
            provider=result.get("provider") or provider.provider_name,
            send_key=provider_name,
            from_address=from_email,
            subject=subject,
        )

        return {
            "status_code": result.get("status_code"),
            "provider": result.get("provider"),
            "from": from_email,
            "to": conversation["supplier_email"],
            "conv_id": conv_id,
            "subject": subject,
            "message_id": sent_message_id,
        }

    @staticmethod
    def _default_subject_line(product_name: str, supplier_type: str) -> str:
        """Build the un-prefixed subject line for a supplier type.

        Args:
            product_name (str): Product being quoted; omitted when empty.
            supplier_type (str): ``"chinese"`` selects the Chinese wording.

        Returns:
            str: e.g. ``"Request for Quotation — Speaker X200"``.

        Example:
            >>> ConversationService._default_subject_line("X200", "chinese")
            'Request for Quotation — X200'
        """
        if (supplier_type or "").strip().lower() == "chinese":
            base = "Request for Quotation"
        else:
            base = "Request for Quotation"
        return f"{base} — {product_name}" if product_name else base

    async def delete_conversation(self, conv_id: str, user_id: str) -> bool:
        """Delete a conversation owned by ``user_id`` and its attachments.

        Verifies ownership before deleting anything, so one user cannot
        delete another user's conversation by guessing its id. Attachment
        files are named ``{conv_id}_...`` on disk (see
        :meth:`~src.webhook_factory.webhook_master.WebhookParserMaster.persist_attachments`),
        so they can be removed with a simple glob.

        Args:
            conv_id (str): The conversation to delete.
            user_id (str): The user requesting the deletion.

        Returns:
            bool: True if the conversation existed and belonged to
                ``user_id`` and was deleted, False otherwise.
        """
        conversation = await self.db.get_conversation(conv_id)
        if not conversation or str(conversation["user_id"]) != str(user_id):
            return False

        for path in Path(self.settings.attachments_dir).glob(f"{conv_id}_*"):
            path.unlink(missing_ok=True)

        await self.db.delete_conversation(conv_id, user_id)
        self.log.info("Deleted conversation %s for user %s", conv_id, user_id)
        return True

    async def delete_user_conversations(self, user_id: str) -> int:
        """Delete every conversation, email and attachment for a user.

        Used by the Email Tracking page's per-user delete action, where
        deleting a user is really "wipe all conversations owned by this
        user" — the user's own row in ``users`` is untouched, so they can
        still start new conversations afterwards.

        Args:
            user_id (str): The user whose entire tracking history should
                be wiped.

        Returns:
            int: The number of conversations deleted.
        """
        conv_ids = await self.db.delete_user_conversations(user_id)
        for conv_id in conv_ids:
            for path in Path(self.settings.attachments_dir).glob(f"{conv_id}_*"):
                path.unlink(missing_ok=True)
        self.log.info(
            "Deleted %d conversation(s) and their attachments for user %s",
            len(conv_ids),
            user_id,
        )
        return len(conv_ids)

    # ── Inbound ──────────────────────────────────────────────────────

    async def handle_inbound(
        self, request: Request, provider_key: str
    ) -> dict:
        """Parse one provider's inbound webhook and run the pipeline.

        Args:
            request (Request): The FastAPI request for the inbound POST.
            provider_key (str): Which provider posted, taken from the URL —
                ``POST /webhooks/inbound/{provider_key}``.

        Returns:
            dict: A status payload — one of ``{"status": "error"}``,
                ``{"status": "rejected", "reason": "invalid_signature"}``,
                ``{"status": "skipped", "reason": "spam"}``, or whatever
                :meth:`process_inbound` returns.

        Example:
            >>> payload = await service.handle_inbound(  # doctest: +SKIP
            ...     req, "engagelab")
            >>> payload["status"]                        # doctest: +SKIP
            'matched'
        """
        try:
            parser = self.get_parser(provider_key)
            inbound = await parser.parse(request)
        except (WebhookParseError, ProviderConfigError) as exc:
            self.log.error("Inbound parse failed (%s): %s", provider_key, exc)
            return {"status": "error", "reason": str(exc)}

        self.log.info(
            "[Inbound:%s] %s -> %s | %s",
            provider_key,
            inbound.from_email,
            inbound.to_email,
            inbound.subject,
        )

        if not inbound.signature_verified:
            self.log.warning("Rejected inbound: signature not verified")
            return {"status": "rejected", "reason": "invalid_signature"}

        if inbound.spam_score > _SPAM_THRESHOLD:
            self.log.info("Skipped inbound: spam score %s", inbound.spam_score)
            return {"status": "skipped", "reason": "spam"}

        return await self.process_inbound(inbound)

    async def process_inbound(self, inbound: InboundEmail) -> dict:
        """Match a normalised inbound email and persist it (requirement 4).

        The single pipeline every inbound path funnels through — webhook
        parsers and the Alibaba IMAP poller alike — so matching behaves
        identically no matter how the mail arrived.

        Args:
            inbound (InboundEmail): The normalised inbound email.

        Returns:
            dict: ``{"status": "duplicate", ...}`` when already ingested,
                ``{"status": "unmatched", "unmatched_id": ...}`` when nothing
                matched, or ``{"status": "matched", "user_id", "conv_id",
                "matched_via", "action"}``.
        """
        # Idempotency: the IMAP poller re-delivers anything it could not
        # finish (and overlaps its backfill window on a cold start), and
        # webhook providers retry on any non-2xx, so the same message arriving
        # twice is routine. Both destinations are checked — ``emails`` has a
        # UNIQUE message_id but ``unmatched_emails`` deliberately does not, so
        # without the second guard a re-delivered cold email would pile up a
        # fresh review row per attempt.
        if inbound.message_id and (
            await self.db.email_exists(inbound.message_id)
            or await self.db.unmatched_email_exists(inbound.message_id)
        ):
            self.log.info(
                "Skipped duplicate inbound message %s", inbound.message_id
            )
            return {"status": "duplicate", "message_id": inbound.message_id}

        conversation, matched_via = await self._match(inbound)

        if not conversation:
            unmatched_id = await self.db.insert_unmatched_email({
                "reason": "no_conversation_match",
                "from_email": inbound.from_email,
                "to_email": inbound.to_email,
                "subject": inbound.subject,
                "body_text": inbound.body_text,
                "body_html": inbound.body_html,
                "provider": inbound.provider,
                "message_id": inbound.message_id,
                "in_reply_to": inbound.in_reply_to,
                "references_header": inbound.references,
                "headers": inbound.headers,
                "spam_score": inbound.spam_score,
                "dkim": inbound.dkim,
                "spf": inbound.spf,
            })
            files = self.webhook.persist_unmatched_attachments(
                inbound.attachments
            )
            await self.db.add_unmatched_attachments(unmatched_id, files)
            self.log.info(
                "Unmatched inbound from %s stored as %s",
                inbound.from_email,
                unmatched_id,
            )
            return {"status": "unmatched", "unmatched_id": unmatched_id}

        conv_id = conversation["conv_id"]
        user_id = conversation["user_id"]
        self.log.info(
            "Matched inbound -> user=%s conv=%s via=%s",
            user_id,
            conv_id,
            matched_via,
        )

        attachments = self.webhook.persist_attachments(
            conv_id, inbound.attachments
        )
        await self.db.add_received_email(conv_id, {
            # A sender with no Message-ID at all is malformed but not worth
            # rejecting; a synthetic id keeps the NOT NULL/UNIQUE column
            # satisfied (at the cost of losing dedup for that one message).
            "message_id": inbound.message_id
            or f"<generated.{uuid.uuid4().hex}@inbound>",
            "in_reply_to": inbound.in_reply_to,
            "references_header": inbound.references,
            "matched_via": matched_via,
            "email_type": self._detect_email_type(inbound.subject),
            "from_email": inbound.from_email,
            "to_email": inbound.to_email,
            "subject": inbound.subject,
            "body_text": inbound.body_text,
            "body_html": inbound.body_html,
            "attachments": attachments,
            "dkim": inbound.dkim,
            "spf": inbound.spf,
            "spam_score": str(inbound.spam_score),
            "provider": inbound.provider,
        })

        action = self._classify_reply(inbound.body_text)
        await self.db.close_conversation(conv_id, last_action=action)

        return {
            "status": "matched",
            "user_id": user_id,
            "conv_id": conv_id,
            "matched_via": matched_via,
            "action": action,
        }

    async def _match(
        self, inbound: InboundEmail
    ) -> tuple[dict | None, str | None]:
        """Resolve the conversation an inbound email belongs to.

        Tries the three strategies in requirement 4's order — see the module
        docstring for why each exists.

        Args:
            inbound (InboundEmail): The normalised inbound email.

        Returns:
            tuple[dict | None, str | None]: The conversation and how it was
                matched (``"message_id"`` / ``"header_token"`` /
                ``"subject_token"``), or ``(None, None)``.
        """
        # 1. In-Reply-To / References → an id this app minted and stored.
        message_ids = EmailMaster.parse_message_ids(
            inbound.in_reply_to, inbound.references
        )
        if message_ids:
            conversation = await self.db.find_conversation_by_message_ids(
                message_ids
            )
            if conversation:
                return conversation, "message_id"

        # 2. Our own custom header, when the client preserved it.
        header_token = (inbound.headers or {}).get(
            _CONVERSATION_ID_HEADER.lower()
        )
        if header_token:
            conversation = await self.db.get_conversation(
                header_token.strip().lower()
            )
            if conversation:
                return conversation, "header_token"

        # 3. The [RFQ - id] subject prefix — the guaranteed fallback.
        subject_token = EmailMaster.parse_conv_id_from_subject(inbound.subject)
        if subject_token:
            conversation = await self.db.get_conversation(subject_token)
            if conversation:
                return conversation, "subject_token"

        return None, None

    @staticmethod
    def _classify_reply(reply_body: str) -> str:
        """Classify a supplier reply with simple keyword matching.

        Purely informational since requirement 4: a matched reply always
        closes the conversation, and this verdict is stored in
        ``conversations.last_action`` rather than driving the status. It
        remains the integration point for a future negotiation agent.

        Args:
            reply_body (str): Plain-text body of the inbound email.

        Returns:
            str: One of ``"QUOTE_RECEIVED"``, ``"DECLINED"``,
                ``"CLARIFICATION_NEEDED"`` or ``"MANUAL_REVIEW"``.

        Example:
            >>> ConversationService._classify_reply("Our price is $11.50")
            'QUOTE_RECEIVED'
        """
        text = (reply_body or "").lower()
        if any(w in text for w in ["price", "quote", "usd", "$", "unit"]):
            return "QUOTE_RECEIVED"
        if any(w in text for w in ["sorry", "cannot", "unable", "no stock"]):
            return "DECLINED"
        if any(w in text for w in ["question", "clarif", "more info", "?"]):
            return "CLARIFICATION_NEEDED"
        return "MANUAL_REVIEW"

    @staticmethod
    def _detect_email_type(subject: str) -> str:
        """Detect whether an inbound email is a reply, forward, or new thread.

        Forward prefixes are tested first: on ``"转发: Re: …"`` the outermost
        action is what the message actually is.

        Chinese webmail (Alibaba, 163, QQ) writes its prefixes with a
        *fullwidth* colon — ``回复：``, not ``回复:`` — so the subject is
        normalised before matching, or every Chinese-client reply would be
        filed as a new thread.

        Args:
            subject (str): The subject line of the inbound email.

        Returns:
            str: One of ``"reply"``, ``"forwarded"``, or ``"new_thread"``.

        Example:
            >>> ConversationService._detect_email_type("转发：[RFQ - abc123]")
            'forwarded'
            >>> ConversationService._detect_email_type("回复：报价")
            'reply'
        """
        s = (subject or "").strip().lower().replace("：", ":")
        if s.startswith((
            "fwd:", "fw:", "fwd ", "fw ",  # Western clients
            "转发:", "轉發:", "转寄:",       # Chinese webmail (simplified / trad.)
        )):
            return "forwarded"
        if s.startswith((
            "re:", "re ",                  # Western clients
            "回复:", "回覆:", "答复:",       # Chinese webmail
        )):
            return "reply"
        return "new_thread"
