"""Async Postgres-backed façade that replaces the old ``EmailDB`` JSON store.

Exposes (almost) the exact same method surface as the old ``src/db.py`` so
:class:`~src.services.conversation_service.ConversationService` needed only
``await`` added at call sites, not a rewrite (per MIGRATION_PLAN.md §2.4).
Every public method opens and closes its own session — callers never see a
:class:`~sqlalchemy.ext.asyncio.AsyncSession`.

Dropped versus the old surface (per MIGRATION_PLAN.md §2.2's notes — these
existed only because JSON has no joins): ``insert_user_conversation``,
``insert_thread``, ``get_thread``, ``get_user_conversation_info``. Plain SQL
joins on ``conversations.user_id`` replace them.
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from src.config import Settings
from src.db.models import (
    Attachment,
    Conversation,
    Email,
    ImapPollState,
    Product,
    UnmatchedAttachment,
    UnmatchedEmail,
    User,
    UserSession,
)


class DuplicateConversationTokenError(Exception):
    """Raised when a freshly generated 8-char conversation token collides.

    The ``conversations.token`` column is ``UNIQUE`` at the DB level (see
    ``sql/schema.sql``); this exception is how that constraint violation
    surfaces so the caller can mint a new token and retry, mirroring the
    collision-retry pattern MIGRATION_PLAN.md §3.2 specifies for
    ``sending_email`` assignment.
    """


class DuplicatePersonalEmailError(Exception):
    """Raised when a registration's ``personal_email`` is already taken."""


class DuplicateSendingEmailError(Exception):
    """Raised when a candidate ``sending_email`` collides with another user's."""


class Repository:
    """Async façade over every table in the Postgres schema.

    Attributes:
        settings (Settings): Shared application configuration.
        log (logging.Logger): Shared application logger.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        logger: logging.Logger,
    ) -> None:
        self._session_factory = session_factory
        self.settings = settings
        self.log = logger

    def _session(self) -> AsyncSession:
        return self._session_factory()

    # ── Users / products (read-only catalog lookups) ───────────────────

    async def get_predefined_users(self) -> list[dict]:
        async with self._session() as session:
            result = await session.execute(select(User).order_by(User.created_at))
            return [self._user_to_dict(u) for u in result.scalars()]

    async def get_user_by_id(self, user_id: str) -> dict | None:
        async with self._session() as session:
            user = await session.get(User, self._as_uuid(user_id))
            return self._user_to_dict(user) if user else None

    async def get_predefined_projects(self) -> list[dict]:
        async with self._session() as session:
            result = await session.execute(select(Product).order_by(Product.created_at))
            return [{"id": str(p.id), "product_name": p.name} for p in result.scalars()]

    async def get_project_by_id(self, project_id: str) -> dict | None:
        async with self._session() as session:
            product = await session.get(Product, self._as_uuid(project_id))
            return {"id": str(product.id), "product_name": product.name} if product else None

    @staticmethod
    def _user_to_dict(user: User) -> dict:
        return {
            "id": str(user.id),
            "full_name": f"{user.first_name} {user.last_name}".strip(),
            "email": user.personal_email,
        }

    # ── Auth: users ──────────────────────────────────────────────────────

    async def create_pending_user(
        self, first_name: str, last_name: str, personal_email: str, password_hash: str
    ) -> dict:
        """Insert a new ``status='pending'`` user (registration wizard step 1).

        Raises:
            DuplicatePersonalEmailError: If ``personal_email`` is already
                registered.
        """
        async with self._session() as session:
            row = User(
                id=uuid.uuid4(),
                first_name=first_name,
                last_name=last_name,
                personal_email=personal_email,
                password_hash=password_hash,
                status="pending",
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if self._is_unique_violation(exc, "uq_users_personal_email"):
                    raise DuplicatePersonalEmailError(personal_email) from exc
                raise
            return self._auth_user_to_dict(row)

    async def get_user_auth_by_personal_email(self, personal_email: str) -> dict | None:
        async with self._session() as session:
            user = (
                await session.execute(
                    select(User).where(User.personal_email == personal_email)
                )
            ).scalar_one_or_none()
            return self._auth_user_to_dict(user) if user else None

    async def get_user_auth_by_id(self, user_id: str) -> dict | None:
        async with self._session() as session:
            user = await session.get(User, self._as_uuid(user_id))
            return self._auth_user_to_dict(user) if user else None

    async def get_user_by_sending_email(self, sending_email: str) -> dict | None:
        """Reverse-lookup the user who owns a permanent ``sending_email``.

        ``sending_email`` is ``UNIQUE`` (``uq_users_sending_email``), so at
        most one user can match. No longer part of inbound matching — that is
        now headers-then-subject-token only (requirement 5) — but kept as the
        one available "who owns this address?" lookup.

        Args:
            sending_email (str): The bare address to look up (case-insensitive).

        Returns:
            dict | None: The auth-shaped user dict (see
                :meth:`_auth_user_to_dict`), or ``None`` if no user has this
                ``sending_email``.
        """
        async with self._session() as session:
            user = (
                await session.execute(
                    select(User).where(
                        func.lower(User.sending_email) == sending_email.lower()
                    )
                )
            ).scalar_one_or_none()
            return self._auth_user_to_dict(user) if user else None

    async def assign_sending_email(self, user_id: str, sending_email: str) -> dict:
        """Finish registration: set the permanent ``sending_email`` and activate.

        Raises:
            DuplicateSendingEmailError: If ``sending_email`` collides with
                another user's (race with a concurrent registration picking
                the same prefix — see MIGRATION_PLAN.md §3.2).
        """
        async with self._session() as session:
            user = await session.get(User, self._as_uuid(user_id))
            if user is None:
                raise ValueError(f"Unknown user_id: {user_id}")
            user.sending_email = sending_email
            user.status = "active"
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if self._is_unique_violation(exc, "uq_users_sending_email"):
                    raise DuplicateSendingEmailError(sending_email) from exc
                raise
            return self._auth_user_to_dict(user)

    @staticmethod
    def _auth_user_to_dict(user: User) -> dict:
        return {
            "id": str(user.id),
            "first_name": user.first_name,
            "last_name": user.last_name,
            "full_name": f"{user.first_name} {user.last_name}".strip(),
            "personal_email": user.personal_email,
            "password_hash": user.password_hash,
            "sending_email": user.sending_email,
            "status": user.status,
            "is_admin": user.is_admin,
        }

    # ── Auth: sessions ───────────────────────────────────────────────────

    async def create_session(
        self,
        user_id: str,
        token_hash: str,
        expires_at: datetime,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> None:
        async with self._session() as session:
            session.add(
                UserSession(
                    id=uuid.uuid4(),
                    user_id=self._as_uuid(user_id),
                    token_hash=token_hash,
                    user_agent=user_agent,
                    ip_address=ip_address,
                    expires_at=expires_at,
                )
            )
            await session.commit()

    async def get_session_user(self, token_hash: str) -> dict | None:
        """Look up the user for a valid, unexpired session and touch it.

        Bumps ``last_seen_at`` (and extends ``expires_at`` if it's less than
        half the configured TTL away) on every call — the sliding-expiry
        behaviour MIGRATION_PLAN.md §3.1 specifies.

        Returns:
            dict | None: The auth user dict, or ``None`` if the token is
                unknown, expired, or its user no longer exists.
        """
        now = datetime.now(timezone.utc)
        async with self._session() as session:
            row = (
                await session.execute(
                    select(UserSession)
                    .options(selectinload(UserSession.user))
                    .where(UserSession.token_hash == token_hash)
                )
            ).scalar_one_or_none()
            if row is None or row.expires_at < now:
                return None
            row.last_seen_at = now
            ttl = row.expires_at - row.created_at
            if row.expires_at - now < ttl / 2:
                row.expires_at = now + ttl
            await session.commit()
            return self._auth_user_to_dict(row.user)

    async def delete_session(self, token_hash: str) -> None:
        async with self._session() as session:
            row = (
                await session.execute(
                    select(UserSession).where(UserSession.token_hash == token_hash)
                )
            ).scalar_one_or_none()
            if row is not None:
                await session.delete(row)
                await session.commit()

    # ── Auth: per-user stats (personal dashboard) ───────────────────────

    async def update_pending_user(
        self,
        user_id: str,
        first_name: str,
        last_name: str,
        personal_email: str,
        password_hash: str,
    ) -> dict:
        """Overwrite a ``status='pending'`` user's step-1 fields (the "Edit" link).

        Raises:
            DuplicatePersonalEmailError: If ``personal_email`` was changed to
                one already registered by a different account.
        """
        async with self._session() as session:
            user = await session.get(User, self._as_uuid(user_id))
            if user is None or user.status != "pending":
                raise ValueError(f"No pending user with id: {user_id}")
            user.first_name = first_name
            user.last_name = last_name
            user.personal_email = personal_email
            user.password_hash = password_hash
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if self._is_unique_violation(exc, "uq_users_personal_email"):
                    raise DuplicatePersonalEmailError(personal_email) from exc
                raise
            return self._auth_user_to_dict(user)

    async def sending_email_exists(self, sending_email: str) -> bool:
        async with self._session() as session:
            result = await session.execute(
                select(User.id).where(User.sending_email == sending_email)
            )
            return result.scalar_one_or_none() is not None

    async def get_user_stats(self, user_id: str) -> dict:
        """Count this user's conversations per lifecycle status.

        Returns:
            dict: ``total`` plus one count per status —  ``draft`` (created
                but not sent), ``open`` (sent, awaiting a reply), ``closed``
                (a reply bound to it) and ``failed`` (the provider rejected
                the send).
        """
        async with self._session() as session:
            row = (
                await session.execute(
                    select(
                        func.count(Conversation.id),
                        func.sum(case((Conversation.status == "draft", 1), else_=0)),
                        func.sum(case((Conversation.status == "open", 1), else_=0)),
                        func.sum(case((Conversation.status == "closed", 1), else_=0)),
                        func.sum(case((Conversation.status == "failed", 1), else_=0)),
                    ).where(Conversation.user_id == self._as_uuid(user_id))
                )
            ).one()
        return {
            "total": row[0] or 0,
            "draft": int(row[1] or 0),
            "open": int(row[2] or 0),
            "closed": int(row[3] or 0),
            "failed": int(row[4] or 0),
        }

    # ── Conversations: write ────────────────────────────────────────────

    async def insert_conversation(self, conversation: dict) -> None:
        """Persist a new conversation, normally as a ``status='draft'`` row.

        Requirement 1: the row exists before any email is sent, so a send
        failure still leaves a durable record of what was attempted.

        Args:
            conversation (dict): The conversation to store. ``conv_id``,
                ``user_id``, ``supplier_name`` and ``supplier_email`` are
                required; ``provider``, ``send_key``, ``from_address`` and
                ``supplier_type`` are all optional at draft time.

        Returns:
            None

        Raises:
            DuplicateConversationTokenError: If ``conversation["conv_id"]``
                (the token) collides with an existing conversation's token.
        """
        async with self._session() as session:
            row = Conversation(
                id=uuid.uuid4(),
                user_id=self._as_uuid(conversation["user_id"]),
                product_id=self._as_uuid(conversation.get("project_id")),
                project_name=conversation.get("project_name") or None,
                product_name=conversation.get("product_name"),
                quantity=self._as_str(conversation.get("quantity")),
                target_price=conversation.get("target_price"),
                supplier_name=conversation["supplier_name"],
                supplier_email=conversation["supplier_email"],
                supplier_type=conversation.get("supplier_type") or None,
                subject=conversation.get("subject") or "",
                token=conversation["conv_id"],
                from_address=conversation.get("from_address") or None,
                provider=conversation.get("provider") or None,
                send_key=conversation.get("send_key") or None,
                status=conversation.get("status", "draft"),
                reply_count=conversation.get("reply_count", 0),
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                if self._is_unique_violation(exc, "uq_conversations_token"):
                    raise DuplicateConversationTokenError(
                        conversation["conv_id"]
                    ) from exc
                raise
        self.log.debug(
            "Inserted conversation %s for user %s (status=%s)",
            conversation["conv_id"],
            conversation["user_id"],
            conversation.get("status", "draft"),
        )

    async def add_sent_email(self, conv_id: str, email_data: dict) -> None:
        """Record the RFQ this app just put on the wire.

        ``email_data["message_id"]`` is used verbatim — it is the real RFC
        header value that was sent, which is what makes the inbound
        ``In-Reply-To``/``References`` lookup resolvable. A duplicate is
        logged and skipped rather than raised, since a retried send is not
        worth failing the request over.

        Args:
            conv_id (str): The conversation this email belongs to.
            email_data (dict): Must carry ``message_id``; optionally
                ``provider_message_id``, ``status_code``, ``from_email``,
                ``to_email``, ``subject``, ``body_html``, ``email_type``,
                ``provider`` and ``attachments``.

        Returns:
            None
        """
        async with self._session() as session:
            conv = await self._get_conversation_row(session, conv_id)
            if conv is None:
                self.log.warning("add_sent_email: unknown conv_id %s", conv_id)
                return
            row = Email(
                id=uuid.uuid4(),
                conversation_id=conv.id,
                direction="sent",
                from_email=email_data.get("from_email", ""),
                to_email=email_data.get("to_email", ""),
                subject=email_data.get("subject", ""),
                body_html=email_data.get("body_html"),
                body_text=email_data.get("body_text"),
                message_id=email_data["message_id"],
                in_reply_to=email_data.get("in_reply_to") or None,
                references_header=email_data.get("references_header") or None,
                reply_type=email_data.get("email_type"),
                matched_via=None,
                provider=email_data.get("provider") or conv.provider or "",
                provider_message_id=email_data.get("provider_message_id"),
                status_code=email_data.get("status_code"),
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError as exc:
                await session.rollback()
                if self._is_unique_violation(exc, "uq_emails_message_id"):
                    self.log.warning(
                        "add_sent_email: message_id %s already recorded on "
                        "%s, skipping",
                        email_data["message_id"],
                        conv_id,
                    )
                    return
                raise
            self._add_attachments(session, row.id, email_data.get("attachments"))
            await session.commit()
        self.log.debug("Recorded sent email on %s", conv_id)

    async def add_received_email(self, conv_id: str, email_data: dict) -> None:
        """Record an inbound reply against its conversation.

        The status transition is **not** made here — the service owns it (see
        :meth:`close_conversation`), so this method only touches the reply
        bookkeeping. ``message_id`` is the sender's own header value, so a
        redelivery of the same message is caught by the UNIQUE constraint and
        skipped.

        Args:
            conv_id (str): The conversation this reply bound to.
            email_data (dict): Must carry ``message_id``; optionally
                ``in_reply_to``, ``references_header``, ``matched_via``,
                bodies, ``dkim``/``spf``/``spam_score`` and ``attachments``.

        Returns:
            None
        """
        async with self._session() as session:
            conv = await self._get_conversation_row(session, conv_id)
            if conv is None:
                self.log.warning("add_received_email: unknown conv_id %s", conv_id)
                return
            row = Email(
                id=uuid.uuid4(),
                conversation_id=conv.id,
                direction="received",
                from_email=email_data.get("from_email", ""),
                to_email=email_data.get("to_email", ""),
                subject=email_data.get("subject", ""),
                body_html=email_data.get("body_html"),
                body_text=email_data.get("body_text"),
                message_id=email_data["message_id"],
                in_reply_to=email_data.get("in_reply_to") or None,
                references_header=email_data.get("references_header") or None,
                reply_type=email_data.get("email_type"),
                matched_via=email_data.get("matched_via"),
                dkim=email_data.get("dkim"),
                spf=email_data.get("spf"),
                spam_score=self._as_float(email_data.get("spam_score")),
                provider=email_data.get("provider") or conv.provider or "",
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError as exc:
                await session.rollback()
                if self._is_unique_violation(exc, "uq_emails_message_id"):
                    # Two concurrent deliveries of the same message can race
                    # past the email_exists() guard; the constraint is the
                    # backstop that keeps it a no-op instead of a 500.
                    self.log.info(
                        "add_received_email: duplicate message_id %s on %s, "
                        "skipping",
                        email_data["message_id"],
                        conv_id,
                    )
                    return
                raise
            self._add_attachments(session, row.id, email_data.get("attachments"))
            conv.reply_count = (conv.reply_count or 0) + 1
            conv.last_reply_at = datetime.now(timezone.utc)
            await session.commit()
        self.log.debug("Recorded inbound reply on %s", conv_id)

    # ── Conversations: lifecycle transitions ────────────────────────────

    async def mark_conversation_sent(
        self,
        conv_id: str,
        *,
        provider: str,
        send_key: str,
        from_address: str,
        subject: str,
    ) -> None:
        """Flip a draft to ``open`` after a successful send (requirement 3).

        Args:
            conv_id (str): The conversation that was just sent.
            provider (str): The provider that accepted the message.
            send_key (str): The resolved factory key, e.g. ``"alibaba_hk"``.
            from_address (str): The address the RFQ actually went out from.
            subject (str): The subject actually sent.

        Returns:
            None
        """
        async with self._session() as session:
            conv = await self._get_conversation_row(session, conv_id)
            if conv is None:
                self.log.warning(
                    "mark_conversation_sent: unknown conv_id %s", conv_id
                )
                return
            conv.status = "open"
            conv.provider = provider or conv.provider
            conv.send_key = send_key or conv.send_key
            conv.from_address = from_address or conv.from_address
            conv.subject = subject or conv.subject
            conv.sent_at = datetime.now(timezone.utc)
            await session.commit()
        self.log.info("Conversation %s: draft -> open", conv_id)

    async def close_conversation(
        self, conv_id: str, *, last_action: str | None = None
    ) -> None:
        """Close a conversation once a reply has bound to it.

        Requirement 4: any matched inbound reply closes the conversation,
        whatever it says. The classifier's verdict is stored alongside in
        ``last_action`` so nothing is lost, but it no longer decides anything.

        Args:
            conv_id (str): The conversation to close.
            last_action (str | None): The reply classifier's verdict.

        Returns:
            None
        """
        async with self._session() as session:
            conv = await self._get_conversation_row(session, conv_id)
            if conv is None:
                self.log.warning(
                    "close_conversation: unknown conv_id %s", conv_id
                )
                return
            conv.status = "closed"
            conv.closed_at = datetime.now(timezone.utc)
            if last_action:
                conv.last_action = last_action
            await session.commit()
        self.log.info(
            "Conversation %s: closed (last_action=%s)", conv_id, last_action
        )

    async def mark_conversation_failed(self, conv_id: str) -> None:
        """Mark a draft as ``failed`` when the provider rejects the send.

        Args:
            conv_id (str): The conversation whose send failed.

        Returns:
            None
        """
        async with self._session() as session:
            conv = await self._get_conversation_row(session, conv_id)
            if conv is None:
                self.log.warning(
                    "mark_conversation_failed: unknown conv_id %s", conv_id
                )
                return
            conv.status = "failed"
            await session.commit()
        self.log.warning("Conversation %s: draft -> failed", conv_id)

    async def update_conversation(self, conv_id: str, updates: dict) -> None:
        async with self._session() as session:
            conv = await self._get_conversation_row(session, conv_id)
            if conv is None:
                self.log.debug("update_conversation: unknown conv_id %s", conv_id)
                return
            for key, value in updates.items():
                if not hasattr(conv, key):
                    continue
                if key == "quantity":
                    value = self._as_str(value)
                setattr(conv, key, value)
            await session.commit()
        self.log.debug("Updated conversation %s", conv_id)

    async def delete_conversation(
        self, conv_id: str, user_id: str | None = None
    ) -> bool:
        async with self._session() as session:
            conv = await self._get_conversation_row(session, conv_id)
            if conv is None:
                return False
            if user_id is not None and str(conv.user_id) != str(user_id):
                return False
            await session.delete(conv)
            await session.commit()
        self.log.info("Deleted conversation %s (user=%s)", conv_id, user_id)
        return True

    async def delete_user_conversations(self, user_id: str) -> list[str]:
        async with self._session() as session:
            result = await session.execute(
                select(Conversation).where(
                    Conversation.user_id == self._as_uuid(user_id)
                )
            )
            convs = list(result.scalars())
            tokens = [c.token for c in convs]
            for conv in convs:
                await session.delete(conv)
            await session.commit()
        self.log.info(
            "Deleted %d conversation(s) for user %s", len(tokens), user_id
        )
        return tokens

    async def insert_unmatched_email(self, email_data: dict) -> str:
        """Persist an inbound email that matched no conversation, in full.

        Requirement 4, step 4. Unlike the old stub-only record, everything
        needed to review (or later re-bind) the message is stored: sender,
        subject, both bodies and the threading headers.

        Args:
            email_data (dict): Normalised inbound fields — ``from_email``,
                ``to_email``, ``subject``, ``body_text``, ``body_html``,
                ``provider``, ``message_id``, ``in_reply_to``,
                ``references_header``, ``received_at`` and ``reason``.

        Returns:
            str: The new ``unmatched_emails.id``, so attachments can be
                attached to it.
        """
        async with self._session() as session:
            row = UnmatchedEmail(
                id=uuid.uuid4(),
                # The full normalised payload is kept as JSONB too, so a
                # field this schema doesn't have a column for is not lost.
                raw_payload=self._jsonable(email_data),
                to_email=email_data.get("to_email"),
                from_email=email_data.get("from_email"),
                subject=email_data.get("subject"),
                body_text=email_data.get("body_text"),
                body_html=email_data.get("body_html"),
                provider=email_data.get("provider"),
                message_id=email_data.get("message_id") or None,
                in_reply_to=email_data.get("in_reply_to") or None,
                references_header=email_data.get("references_header") or None,
                reason=email_data.get("reason", "unmatched"),
                status="needs_review",
                received_at=datetime.now(timezone.utc),
            )
            session.add(row)
            await session.commit()
            unmatched_id = str(row.id)
        self.log.info(
            "Stored unmatched inbound email from %s to %s (id=%s)",
            email_data.get("from_email"),
            email_data.get("to_email"),
            unmatched_id,
        )
        return unmatched_id

    async def add_unmatched_attachments(
        self, unmatched_email_id: str, attachments: list[dict]
    ) -> None:
        """Attach already-persisted files to an unmatched email record.

        Args:
            unmatched_email_id (str): The ``unmatched_emails.id`` returned by
                :meth:`insert_unmatched_email`.
            attachments (list[dict]): Metadata dicts as returned by
                :meth:`~src.webhook_factory.webhook_master.WebhookParserMaster.persist_unmatched_attachments`.

        Returns:
            None
        """
        if not attachments:
            return
        async with self._session() as session:
            for att in attachments:
                session.add(
                    UnmatchedAttachment(
                        id=uuid.uuid4(),
                        unmatched_email_id=self._as_uuid(unmatched_email_id),
                        filename=att.get("filename", "attachment"),
                        url=att.get("url", ""),
                        content_type=att.get("content_type"),
                        size_bytes=att.get("size"),
                    )
                )
            await session.commit()
        self.log.debug(
            "Stored %d attachment(s) on unmatched email %s",
            len(attachments),
            unmatched_email_id,
        )

    # ── Conversations: read ─────────────────────────────────────────────

    async def get_conversation(self, conv_id: str) -> dict | None:
        async with self._session() as session:
            conv = await self._get_conversation_row(
                session, conv_id, with_emails=True
            )
            return self._conversation_to_dict(conv, include_emails=True) if conv else None

    async def get_user_conversations(self, user_id: str) -> list[dict]:
        async with self._session() as session:
            result = await session.execute(
                select(Conversation)
                .where(Conversation.user_id == self._as_uuid(user_id))
                .order_by(Conversation.created_at.desc())
            )
            return [
                self._conversation_to_dict(c, include_emails=False)
                for c in result.scalars()
            ]

    async def find_conversation_by_message_ids(
        self, message_ids: list[str]
    ) -> dict | None:
        """Resolve a conversation from RFC ``Message-ID`` values.

        The primary inbound matching strategy (requirement 4, step 1): a
        reply's ``In-Reply-To``/``References`` name the ids of the messages
        it answers, and this app minted (and stored) the id of every RFQ it
        sent, so one join resolves the thread.

        Caller order is preserved deliberately — the service passes the
        most-recent reference first, so on a long thread the newest matching
        conversation wins rather than an arbitrary one.

        Args:
            message_ids (list[str]): Angle-bracketed ids, most recent first.

        Returns:
            dict | None: The matching conversation, or ``None`` if none of
                the ids are ours.
        """
        if not message_ids:
            return None
        async with self._session() as session:
            rows = (
                await session.execute(
                    select(Email.message_id, Conversation)
                    .join(Conversation, Email.conversation_id == Conversation.id)
                    .where(Email.message_id.in_(message_ids))
                )
            ).all()
            if not rows:
                return None
            by_message_id = {row[0]: row[1] for row in rows}
            for message_id in message_ids:
                conv = by_message_id.get(message_id)
                if conv is not None:
                    return self._conversation_to_dict(conv, include_emails=False)
            return None

    async def email_exists(self, message_id: str) -> bool:
        """Return whether an email with this ``Message-ID`` is already stored.

        The inbound idempotency guard: IMAP polling re-delivers anything not
        yet flagged, and webhook providers retry on any non-2xx, so the same
        message reaching :meth:`add_received_email` twice is routine rather
        than exceptional.

        Args:
            message_id (str): The RFC ``Message-ID`` to check.

        Returns:
            bool: ``True`` if that id is already recorded.
        """
        if not message_id:
            return False
        async with self._session() as session:
            result = await session.execute(
                select(Email.id).where(Email.message_id == message_id).limit(1)
            )
            return result.scalar_one_or_none() is not None

    async def unmatched_email_exists(self, message_id: str) -> bool:
        """Return whether this ``Message-ID`` is already parked as unmatched.

        The second half of the inbound idempotency guard. ``emails`` has a
        UNIQUE ``message_id``, so a re-delivered *matched* reply can never
        duplicate; ``unmatched_emails`` deliberately does not (an unmatched
        message may legitimately be re-filed later), which means re-delivery
        would otherwise append a fresh review row every time. The IMAP poller
        re-delivers on any failure, so this is a routine case, not a rare one.

        Args:
            message_id (str): The RFC ``Message-ID`` to check.

        Returns:
            bool: ``True`` if that id is already parked for review.
        """
        if not message_id:
            return False
        async with self._session() as session:
            result = await session.execute(
                select(UnmatchedEmail.id)
                .where(UnmatchedEmail.message_id == message_id)
                .limit(1)
            )
            return result.scalar_one_or_none() is not None

    # ── IMAP poll cursor ───────────────────────────────────────────────

    async def get_imap_cursor(self, account: str, mailbox: str) -> dict | None:
        """Return how far the poller has read one mailbox, if known.

        Args:
            account (str): The mailbox address being polled.
            mailbox (str): The folder being polled, e.g. ``"INBOX"``.

        Returns:
            dict | None: ``{"uid_validity": str, "last_uid": int}``, or
                ``None`` on the very first poll of this mailbox.
        """
        async with self._session() as session:
            row = (
                await session.execute(
                    select(ImapPollState).where(
                        ImapPollState.account == account,
                        ImapPollState.mailbox == mailbox,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "uid_validity": row.uid_validity,
                "last_uid": int(row.last_uid or 0),
            }

    async def save_imap_cursor(
        self, account: str, mailbox: str, *, uid_validity: str, last_uid: int
    ) -> None:
        """Advance (or reset) the poll cursor for one mailbox.

        Written as an upsert so the first poll and every later one take the
        same path, and so two replicas polling the same mailbox cannot race
        into a duplicate-key error.

        The cursor only ever moves forward *within* a ``UIDVALIDITY``
        generation: a lower ``last_uid`` for the same generation is ignored,
        which keeps a stale in-flight batch from rewinding a cursor another
        poll already advanced. A changed generation replaces it outright,
        since the old UIDs no longer mean anything.

        Args:
            account (str): The mailbox address being polled.
            mailbox (str): The folder being polled.
            uid_validity (str): The mailbox's current ``UIDVALIDITY``.
            last_uid (int): Highest UID fully processed.

        Returns:
            None
        """
        async with self._session() as session:
            statement = pg_insert(ImapPollState).values(
                id=uuid.uuid4(),
                account=account,
                mailbox=mailbox,
                uid_validity=uid_validity,
                last_uid=last_uid,
                updated_at=datetime.now(timezone.utc),
            )
            statement = statement.on_conflict_do_update(
                constraint="uq_imap_poll_state_account_mailbox",
                set_={
                    "uid_validity": statement.excluded.uid_validity,
                    "last_uid": statement.excluded.last_uid,
                    "updated_at": statement.excluded.updated_at,
                },
                where=(
                    (ImapPollState.uid_validity != statement.excluded.uid_validity)
                    | (ImapPollState.last_uid < statement.excluded.last_uid)
                ),
            )
            await session.execute(statement)
            await session.commit()
        self.log.debug(
            "IMAP cursor %s/%s -> uid %s (validity %s)",
            account,
            mailbox,
            last_uid,
            uid_validity,
        )

    async def get_all_users(self) -> list[dict]:
        async with self._session() as session:
            stmt = (
                select(
                    User.id,
                    User.first_name,
                    User.last_name,
                    User.personal_email,
                    func.count(Conversation.id).label("conversation_count"),
                    func.sum(
                        case((Conversation.status == "closed", 1), else_=0)
                    ).label("replied_count"),
                    func.sum(
                        case((Conversation.status == "open", 1), else_=0)
                    ).label("open_count"),
                    func.max(
                        func.coalesce(
                            Conversation.last_reply_at, Conversation.created_at
                        )
                    ).label("last_activity"),
                )
                .join(Conversation, Conversation.user_id == User.id)
                .group_by(User.id, User.first_name, User.last_name, User.personal_email)
            )
            rows = (await session.execute(stmt)).all()
        result = [
            {
                "user_id": str(row.id),
                "user_name": f"{row.first_name} {row.last_name}".strip(),
                "user_email": row.personal_email,
                "conversation_count": row.conversation_count,
                "replied_count": int(row.replied_count or 0),
                "open_count": int(row.open_count or 0),
                "last_activity": row.last_activity.isoformat()
                if row.last_activity
                else "",
            }
            for row in rows
        ]
        result.sort(key=lambda u: u["last_activity"], reverse=True)
        return result

    async def get_stats(self) -> dict:
        """Site-wide conversation counters.

        ``total_replied`` counts ``closed`` conversations — a conversation
        closes precisely when a supplier reply binds to it, so the two mean
        the same thing under the new lifecycle.
        """
        async with self._session() as session:
            row = (
                await session.execute(
                    select(
                        func.count(func.distinct(Conversation.user_id)),
                        func.count(Conversation.id),
                        func.sum(
                            case((Conversation.status == "closed", 1), else_=0)
                        ),
                        func.sum(
                            case((Conversation.status == "open", 1), else_=0)
                        ),
                        func.sum(
                            case((Conversation.status == "draft", 1), else_=0)
                        ),
                    )
                )
            ).one()
        return {
            "total_users": row[0] or 0,
            "total_conversations": row[1] or 0,
            "total_replied": int(row[2] or 0),
            "total_open": int(row[3] or 0),
            "total_draft": int(row[4] or 0),
        }

    # ── Internal helpers ─────────────────────────────────────────────────

    async def _get_conversation_row(
        self, session: AsyncSession, conv_id: str, with_emails: bool = False
    ) -> Conversation | None:
        stmt = select(Conversation).where(Conversation.token == conv_id)
        if with_emails:
            stmt = stmt.options(
                selectinload(Conversation.user),
                selectinload(Conversation.emails).selectinload(Email.attachments),
            )
        return (await session.execute(stmt)).scalar_one_or_none()

    def _conversation_to_dict(
        self, conv: Conversation, include_emails: bool
    ) -> dict:
        user_name = ""
        if include_emails and conv.user is not None:
            user_name = f"{conv.user.first_name} {conv.user.last_name}".strip()
        result = {
            "conv_id": conv.token,
            "thread_id": conv.token,
            "project_id": str(conv.product_id) if conv.product_id else "",
            "project_name": conv.project_name or "",
            "user_id": str(conv.user_id),
            "user_name": user_name,
            "supplier_email": conv.supplier_email,
            "supplier_name": conv.supplier_name,
            "supplier_type": conv.supplier_type or "",
            "from_address": conv.from_address or "",
            "provider": conv.provider or "",
            "send_key": conv.send_key or "",
            "status": conv.status,
            "last_action": conv.last_action or "",
            "created_at": conv.created_at.isoformat(),
            "reply_count": conv.reply_count,
            "last_reply_at": conv.last_reply_at.isoformat()
            if conv.last_reply_at
            else None,
            "sent_at": conv.sent_at.isoformat() if conv.sent_at else None,
            "closed_at": conv.closed_at.isoformat() if conv.closed_at else None,
            "product_name": conv.product_name,
            "quantity": conv.quantity,
            "target_price": conv.target_price,
            "subject": conv.subject,
            "emails_sent": [],
            "emails_received": [],
        }
        if include_emails:
            for email in sorted(conv.emails, key=lambda e: e.created_at):
                key = "emails_sent" if email.direction == "sent" else "emails_received"
                result[key].append(self._email_to_dict(email))
        return result

    @staticmethod
    def _email_to_dict(email: Email) -> dict:
        ts_field = "sent_at" if email.direction == "sent" else "received_at"
        result = {
            "email_type": email.reply_type,
            "from_email": email.from_email,
            "to_email": email.to_email,
            "subject": email.subject,
            "body_html": email.body_html,
            "body_text": email.body_text,
            "message_id": email.message_id,
            "attachments": [
                {
                    "filename": a.filename,
                    "content_type": a.content_type,
                    "size": a.size_bytes,
                    "url": a.url,
                }
                for a in email.attachments
            ],
            ts_field: email.created_at.isoformat(),
        }
        if email.direction == "sent":
            result["provider_message_id"] = email.provider_message_id
            result["status_code"] = email.status_code
        else:
            # matched_via makes threading bugs obvious at a glance in the UI:
            # 'message_id' means the headers survived, 'subject_token' means
            # they didn't and the subject prefix did the work.
            result["matched_via"] = email.matched_via
            result["dkim"] = email.dkim
            result["spf"] = email.spf
            result["spam_score"] = (
                str(email.spam_score) if email.spam_score is not None else None
            )
        return result

    @staticmethod
    def _add_attachments(
        session: AsyncSession, email_id: uuid.UUID, attachments: list[dict] | None
    ) -> None:
        for att in attachments or []:
            session.add(
                Attachment(
                    id=uuid.uuid4(),
                    email_id=email_id,
                    filename=att.get("filename", "attachment"),
                    url=att.get("url", ""),
                    content_type=att.get("content_type"),
                    size_bytes=att.get("size"),
                )
            )

    @staticmethod
    def _jsonable(value):
        """Coerce a payload into something the JSONB column can store.

        Inbound payloads carry ``datetime`` values and, occasionally, raw
        ``bytes``; both make asyncpg's JSON encoder raise. Anything it does
        not recognise is stringified rather than dropped, since this column
        exists precisely so nothing is lost.
        """
        if isinstance(value, dict):
            return {str(k): Repository._jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [Repository._jsonable(v) for v in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value)

    @staticmethod
    def _as_uuid(value: str | None) -> uuid.UUID | None:
        if not value:
            return None
        return uuid.UUID(str(value))

    @staticmethod
    def _as_str(value) -> str | None:
        return None if value is None else str(value)

    @staticmethod
    def _as_float(value) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_unique_violation(exc: IntegrityError, constraint_name: str) -> bool:
        orig = getattr(exc, "orig", None)
        sqlstate = getattr(orig, "sqlstate", None)
        return sqlstate == "23505" and constraint_name in str(orig)
