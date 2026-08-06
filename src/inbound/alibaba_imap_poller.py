"""Background IMAP poller for Alibaba Enterprise Mail inbound replies.

Alibaba Enterprise Mail offers no inbound webhook, so replies are pulled
instead of pushed: one :class:`AlibabaImapPoller` per configured region
(Singapore and/or Hong Kong) logs into the mailbox every
``ALIBABA_POLL_INTERVAL_SECONDS``, fetches everything ``UNSEEN``, converts
each message to an
:class:`~src.webhook_factory.webhook_master.InboundEmail` via
:class:`~src.webhook_factory.alibaba_webhook.AlibabaWebhookParser`, and feeds
it to the same
:meth:`~src.services.conversation_service.ConversationService.process_inbound`
pipeline every webhook uses. Nothing downstream can tell the difference.

**Crash safety.** Messages are fetched with ``BODY.PEEK[]``, which does *not*
set ``\\Seen``; the flag is set only after :meth:`process_inbound` returns.
A crash mid-processing therefore re-delivers the message on the next poll,
and the ``email_exists`` idempotency guard makes that a no-op.

.. warning::

   With more than one app replica, every replica polls the same mailbox and
   does the same work (safe, thanks to the guard above, but wasteful). For
   the POC, run the poller on a single instance — set
   ``ALIBABA_INBOUND_ENABLED=false`` on the others — or move it to a
   dedicated worker.

:mod:`imaplib` is entirely blocking, so every IMAP call runs through
:func:`asyncio.to_thread`.

Example:
    >>> pollers = build_alibaba_pollers(       # doctest: +SKIP
    ...     service, settings, logger)
    >>> await pollers[0].poll_once()           # doctest: +SKIP
    2
"""

import asyncio
import email
import imaplib
import logging
from email.policy import default as email_default_policy

from src.config import Settings
from src.webhook_factory.alibaba_webhook import AlibabaWebhookParser

# IMAP socket timeout in seconds — both endpoints are remote from most
# deployments, and a hung socket would stall the whole poll loop.
_IMAP_TIMEOUT = 30

# Cap on messages processed per poll. A backlog is drained across successive
# polls rather than in one long-running pass, so a mailbox with thousands of
# unread messages can't monopolise the loop (or the DB) on startup.
_MAX_PER_POLL = 50


class AlibabaImapPoller:
    """Poll one Alibaba mailbox and feed replies into the inbound pipeline.

    Attributes:
        service: The :class:`~src.services.conversation_service.ConversationService`
            whose ``process_inbound`` each fetched message is handed to.
        settings (Settings): Shared application configuration.
        log (logging.Logger): Shared application logger.
        region_key (str): ``"alibaba"`` or ``"alibaba_hk"`` — recorded as the
            inbound email's provider.

    Example:
        >>> poller = AlibabaImapPoller(          # doctest: +SKIP
        ...     service, settings, logger, host="imap.qiye.aliyun.com",
        ...     port=993, address="a@b.com", password="…",
        ...     mailbox="INBOX", region_key="alibaba")
    """

    def __init__(
        self,
        service,
        settings: Settings,
        logger: logging.Logger,
        *,
        host: str,
        port: int,
        address: str,
        password: str,
        mailbox: str,
        region_key: str,
        processed_mailbox: str = "",
    ) -> None:
        """Store the mailbox connection details for one region.

        Args:
            service: The conversation service to hand messages to.
            settings (Settings): Shared application configuration.
            logger (logging.Logger): Shared application logger.
            host (str): IMAP host for this region.
            port (int): IMAP port (993 = implicit SSL).
            address (str): Mailbox address to log in as.
            password (str): The third-party client security password.
            mailbox (str): Folder to poll, normally ``"INBOX"``.
            region_key (str): ``"alibaba"`` or ``"alibaba_hk"``.
            processed_mailbox (str): Optional folder to move handled messages
                into; when empty they are only flagged ``\\Seen``.

        Returns:
            None
        """
        self.service = service
        self.settings = settings
        self.log = logger
        self.host = host
        self.port = port
        self.address = address
        self.password = password
        self.mailbox = mailbox or "INBOX"
        self.region_key = region_key
        self.processed_mailbox = processed_mailbox
        self._adapter = AlibabaWebhookParser(settings, logger)

    async def run_forever(self) -> None:
        """Poll in a loop until the task is cancelled.

        Every failure is caught and logged: one unreachable poll (network
        blip, IMAP hiccup) must never kill the loop, or inbound silently
        stops for the whole process lifetime.

        Returns:
            None
        """
        interval = self.settings.alibaba_poll_interval_seconds
        self.log.info(
            "Alibaba IMAP poller started (%s, %s@%s, every %ss)",
            self.region_key,
            self.mailbox,
            self.host,
            interval,
        )
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                self.log.info(
                    "Alibaba IMAP poller stopping (%s)", self.region_key
                )
                raise
            except Exception:  # noqa: BLE001 - one bad poll must not stop all
                self.log.exception(
                    "Alibaba IMAP poll failed (%s)", self.region_key
                )
            await asyncio.sleep(interval)

    async def poll_once(self) -> int:
        """Fetch and process one batch of unseen messages.

        Returns:
            int: How many messages were fetched this round.
        """
        messages = await asyncio.to_thread(self._fetch_unseen)
        if not messages:
            return 0

        self.log.info(
            "Alibaba IMAP (%s): processing %d unseen message(s)",
            self.region_key,
            len(messages),
        )
        for uid, raw in messages:
            try:
                msg = email.message_from_bytes(
                    raw, policy=email_default_policy
                )
                inbound = self._adapter.from_mime(
                    msg, raw.decode("utf-8", "replace")
                )
                # Distinguish the two regions downstream even though both
                # share the "alibaba" provider name.
                inbound.provider = self.region_key
                result = await self.service.process_inbound(inbound)
                self.log.info(
                    "Alibaba IMAP (%s): uid=%s -> %s",
                    self.region_key,
                    uid.decode() if isinstance(uid, bytes) else uid,
                    result.get("status"),
                )
            except Exception:  # noqa: BLE001 - keep draining the batch
                # Left unflagged deliberately: the next poll retries it.
                self.log.exception(
                    "Alibaba IMAP (%s): failed to process uid %s",
                    self.region_key,
                    uid,
                )
                continue
            # Only now is it safe to mark handled.
            await asyncio.to_thread(self._mark_processed, uid)
        return len(messages)

    # ── Blocking IMAP calls (always run via asyncio.to_thread) ───────

    def _connect(self) -> imaplib.IMAP4_SSL:
        """Open an authenticated IMAP connection to this region's mailbox.

        Returns:
            imaplib.IMAP4_SSL: A logged-in client with ``mailbox`` selected.
        """
        client = imaplib.IMAP4_SSL(self.host, self.port, timeout=_IMAP_TIMEOUT)
        client.login(self.address, self.password)
        client.select(self.mailbox)
        return client

    def _fetch_unseen(self) -> list[tuple[bytes, bytes]]:
        """Fetch up to :data:`_MAX_PER_POLL` unseen messages, without flagging.

        ``BODY.PEEK[]`` is deliberate: it returns the full source *without*
        setting ``\\Seen``, so a message is only considered handled once
        processing actually succeeded.

        Returns:
            list[tuple[bytes, bytes]]: ``(uid, raw_bytes)`` pairs; empty on
                any failure (logged, never raised, so the loop continues).
        """
        client = None
        messages: list[tuple[bytes, bytes]] = []
        try:
            client = self._connect()
            status, data = client.uid("SEARCH", None, "UNSEEN")
            if status != "OK":
                self.log.error(
                    "Alibaba IMAP (%s): SEARCH returned %s",
                    self.region_key,
                    status,
                )
                return []

            uids = data[0].split() if data and data[0] else []
            if len(uids) > _MAX_PER_POLL:
                self.log.info(
                    "Alibaba IMAP (%s): %d unseen, processing the oldest %d "
                    "this round",
                    self.region_key,
                    len(uids),
                    _MAX_PER_POLL,
                )
                uids = uids[:_MAX_PER_POLL]

            for uid in uids:
                status, payload = client.uid("FETCH", uid, "(BODY.PEEK[])")
                if status != "OK" or not payload or not payload[0]:
                    self.log.warning(
                        "Alibaba IMAP (%s): FETCH failed for uid %s",
                        self.region_key,
                        uid,
                    )
                    continue
                messages.append((uid, payload[0][1]))
        except (imaplib.IMAP4.error, OSError) as exc:
            self.log.error(
                "Alibaba IMAP (%s) fetch failed: %s", self.region_key, exc
            )
            return []
        finally:
            self._close(client)
        return messages

    def _mark_processed(self, uid: bytes) -> None:
        """Flag a message ``\\Seen``, optionally archiving it.

        Called only after :meth:`process_inbound` succeeded, so an unflagged
        message always means "not yet handled".

        Args:
            uid (bytes): The message UID to mark.

        Returns:
            None
        """
        client = None
        try:
            client = self._connect()
            if self.processed_mailbox:
                client.uid("COPY", uid, self.processed_mailbox)
                client.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
                client.expunge()
            else:
                client.uid("STORE", uid, "+FLAGS", r"(\Seen)")
        except (imaplib.IMAP4.error, OSError) as exc:
            # Not fatal: the message is simply re-delivered next poll, where
            # the email_exists guard turns it into a no-op.
            self.log.error(
                "Alibaba IMAP (%s): could not flag uid %s: %s",
                self.region_key,
                uid,
                exc,
            )
        finally:
            self._close(client)

    def _close(self, client: imaplib.IMAP4_SSL | None) -> None:
        """Log out, swallowing the noise a half-open connection produces.

        Args:
            client (imaplib.IMAP4_SSL | None): The client to close.

        Returns:
            None
        """
        if client is None:
            return
        try:
            client.logout()
        except (imaplib.IMAP4.error, OSError):
            self.log.debug(
                "Alibaba IMAP (%s): logout failed, connection dropped",
                self.region_key,
            )


def build_alibaba_pollers(
    service, settings: Settings, logger: logging.Logger
) -> list[AlibabaImapPoller]:
    """Build one poller per configured Alibaba region.

    A region is included only when it has a mailbox address *and* a password
    configured, so a deployment that uses just one region doesn't start a
    second poller that would fail on every tick.

    Regions are deduplicated by ``(address, mailbox)`` and deliberately *not*
    by host: ``imap.qiye.aliyun.com`` and ``imaphk.qiye.aliyun.com`` are
    regional access points to the same Alibaba mailbox, so with the default
    configuration — where the ``ALIBABA_HK_*`` credentials fall back to the
    Singapore ones — two pollers would otherwise race over the same messages
    and both try to flag them. One mailbox, one poller.

    Args:
        service: The conversation service to hand messages to.
        settings (Settings): Shared application configuration.
        logger (logging.Logger): Shared application logger.

    Returns:
        list[AlibabaImapPoller]: Zero, one or two pollers, ready to run.
    """
    regions = [
        {
            "region_key": "alibaba",
            "host": settings.alibaba_imap_host,
            "port": settings.alibaba_imap_port,
            "address": settings.alibaba_mail_address,
            "password": settings.alibaba_mail_password,
            "mailbox": settings.alibaba_imap_mailbox,
            "processed_mailbox": settings.alibaba_imap_processed_mailbox,
        },
        {
            "region_key": "alibaba_hk",
            "host": settings.alibaba_hk_imap_host,
            "port": settings.alibaba_hk_imap_port,
            "address": settings.alibaba_hk_mail_address,
            "password": settings.alibaba_hk_mail_password,
            "mailbox": settings.alibaba_hk_imap_mailbox,
            "processed_mailbox": settings.alibaba_hk_imap_processed_mailbox,
        },
    ]

    pollers: list[AlibabaImapPoller] = []
    seen: set[tuple[str, str]] = set()
    for region in regions:
        if not (region["address"] and region["password"]):
            logger.info(
                "Alibaba IMAP poller for %s not configured, skipping",
                region["region_key"],
            )
            continue
        identity = (region["address"].strip().lower(), region["mailbox"])
        if identity in seen:
            logger.info(
                "Alibaba IMAP poller for %s polls the same mailbox (%s/%s) as "
                "an already-configured region, skipping the duplicate",
                region["region_key"],
                region["address"],
                region["mailbox"],
            )
            continue
        seen.add(identity)
        pollers.append(
            AlibabaImapPoller(service, settings, logger, **region)
        )
    return pollers
