"""Background IMAP poller for Alibaba Enterprise Mail inbound replies.

Alibaba Enterprise Mail (the provider) offers no inbound webhook, so replies
are pulled instead of pushed: one :class:`AlibabaImapPoller` per configured
server (Singapore and/or Hong Kong) logs into the mailbox every
``ALIBABA_POLL_INTERVAL_SECONDS``, fetches every message that has arrived
since its last successful poll, converts each to an
:class:`~src.webhook_factory.webhook_master.InboundEmail` via
:class:`~src.webhook_factory.alibaba_webhook.AlibabaWebhookParser`, and feeds
it to the same
:meth:`~src.services.conversation_service.ConversationService.process_inbound`
pipeline every webhook uses. Nothing downstream can tell the difference.

**"Since its last poll" means a UID cursor, not the read flag.** This poller
used to ``SEARCH UNSEEN``, which made the ``\\Seen`` flag load-bearing: any
message a human opened first — the webmail preview pane is enough, and a
phone client syncing the same mailbox does it unprompted — was never fetched
at all, leaving no trace in ``emails`` *or* ``unmatched_emails``. Replies
survived only by winning a race against whoever was watching the inbox.

Instead, the highest fully-processed UID is persisted per mailbox in
``imap_poll_state`` (see :class:`~src.db.models.ImapPollState`) and each poll
asks for ``UID {last+1}:*``. IMAP UIDs increase monotonically within a
``UIDVALIDITY`` generation, so that range is an exact definition of "new
mail" that no amount of reading, flagging or foldering can perturb. On the
first ever poll of a mailbox — or after the server renumbers it, which
``UIDVALIDITY`` announces — the poller bootstraps from the last
``ALIBABA_IMAP_BACKFILL_DAYS`` days rather than trawling the whole history.

**Crash safety.** Messages are fetched with ``BODY.PEEK[]``, so reading them
changes nothing server-side, and the cursor only advances across an unbroken
run of successes: a message that raises holds the cursor at the UID *before*
it, so the next poll retries it. Re-delivery is safe because
``email_exists`` / ``unmatched_email_exists`` make a repeat ingest a no-op.
A message that keeps failing is skipped after :data:`_MAX_UID_ATTEMPTS`
tries, with an error logged — one malformed message must not wedge inbound
mail behind it forever.

.. warning::

   With more than one app replica, every replica polls the same mailbox and
   does the same work (safe, thanks to the guards above, but wasteful). For
   the POC, run the poller on a single instance — set the region-specific
   polling flags (``ALIBABA_IMAP_POLLING_ENABLED``,
   ``ALIBABA_HK_IMAP_POLLING_ENABLED``) to ``false`` on the others, or move
   polling to a dedicated worker.

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
from datetime import datetime, timedelta, timezone
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

# How many times a single UID may fail before the cursor steps over it. Without
# this, one message that always raises (unparseable MIME, an attachment the
# filesystem rejects) parks the cursor in front of it and every later reply
# queues up behind it, unseen, forever.
_MAX_UID_ATTEMPTS = 3


class AlibabaImapPoller:
    """Poll one Alibaba mailbox and feed replies into the inbound pipeline.

    Attributes:
        service: The :class:`~src.services.conversation_service.ConversationService`
            whose ``process_inbound`` each fetched message is handed to.
        db (Repository): The same service's persistence layer, used directly
            for the poll cursor (``imap_poll_state``) — mailbox read position
            is the poller's own bookkeeping, not conversation state.
        settings (Settings): Shared application configuration.
        log (logging.Logger): Shared application logger.
        region_key (str): ``"alibaba"`` (Singapore server) or ``"alibaba_hk"``
            (Hong Kong server) — recorded as the inbound email's provider.

    Example:
        >>> poller = AlibabaImapPoller(          # doctest: +SKIP
        ...     service, settings, logger, host="imap.sg.aliyun.com",
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
        self.db = service.db
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
        # uid → consecutive failures, so a permanently broken message can be
        # stepped over instead of blocking every UID behind it. In-memory
        # deliberately: a restart is itself worth one more attempt.
        self._attempts: dict[int, int] = {}

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
        """Fetch and process everything that arrived since the last poll.

        "Since the last poll" is the stored UID cursor, never the ``\\Seen``
        flag, so a message already opened in webmail is still ingested.

        The cursor advances only across an unbroken run of successfully
        processed UIDs: the first failure holds it in place (so the next poll
        retries that message) while the rest of the batch is still drained —
        re-processing is a no-op thanks to the duplicate guards. After
        :data:`_MAX_UID_ATTEMPTS` failures on the same UID the cursor steps
        over it, loudly, rather than letting one bad message wedge inbound.

        Returns:
            int: How many messages were fetched this round.
        """
        cursor = await self.db.get_imap_cursor(self.address, self.mailbox)
        batch = await asyncio.to_thread(self._fetch_new, cursor)
        if batch is None:
            return 0

        floor: int = batch["floor"]
        watermark = floor
        messages: list[tuple[int, bytes | None]] = batch["messages"]

        if not messages:
            # A bootstrap that found nothing still needs recording, or the
            # poller re-derives the same baseline every interval forever.
            if batch["bootstrapped"]:
                await self._save_cursor(batch["uid_validity"], floor)
            return 0

        self.log.info(
            "Alibaba IMAP (%s): processing %d message(s) after uid %s",
            self.region_key,
            len(messages),
            floor,
        )
        # Once a UID fails, later ones may still be processed, but the cursor
        # must not move past the failure or that message is lost.
        blocked = False
        for uid, raw in messages:
            if raw is None:
                # Undownloadable and out of retries; _fetch_new already
                # logged it. Let the cursor past so the queue keeps moving.
                if not blocked:
                    watermark = uid
                continue
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
                    uid,
                    result.get("status"),
                )
            except Exception:  # noqa: BLE001 - keep draining the batch
                self.log.exception(
                    "Alibaba IMAP (%s): failed to process uid %s",
                    self.region_key,
                    uid,
                )
                if self._give_up(uid):
                    if not blocked:
                        watermark = uid
                else:
                    blocked = True
                continue
            self._attempts.pop(uid, None)
            if not blocked:
                watermark = uid
            # Cosmetic now that the cursor decides what is due: it keeps the
            # mailbox tidy for whoever looks at it, and drives the optional
            # move into ALIBABA_IMAP_PROCESSED_MAILBOX.
            await asyncio.to_thread(self._mark_processed, uid)

        if watermark > floor or batch["bootstrapped"]:
            await self._save_cursor(batch["uid_validity"], watermark)
        return len(messages)

    def _give_up(self, uid: int) -> bool:
        """Count a failure on ``uid`` and report whether to skip it for good.

        Args:
            uid (int): The UID that just failed to process.

        Returns:
            bool: ``True`` once this UID has failed
                :data:`_MAX_UID_ATTEMPTS` times, meaning the cursor should
                move past it instead of retrying forever.
        """
        attempts = self._attempts.get(uid, 0) + 1
        self._attempts[uid] = attempts
        if attempts < _MAX_UID_ATTEMPTS:
            return False
        self._attempts.pop(uid, None)
        self.log.error(
            "Alibaba IMAP (%s): giving up on uid %s after %d attempts — "
            "skipping it so later mail is not blocked. Inspect it by hand in "
            "%s.",
            self.region_key,
            uid,
            attempts,
            self.mailbox,
        )
        return True

    async def _save_cursor(self, uid_validity: str, last_uid: int) -> None:
        """Persist the poll cursor, tolerating a DB hiccup.

        A failed write is not fatal: the cursor stays where it was and the
        next poll re-delivers the same messages, which the duplicate guards
        absorb. Losing inbound mail would be far worse than repeating it.

        Args:
            uid_validity (str): The mailbox's current ``UIDVALIDITY``.
            last_uid (int): Highest UID fully processed.

        Returns:
            None
        """
        try:
            await self.db.save_imap_cursor(
                self.address,
                self.mailbox,
                uid_validity=uid_validity,
                last_uid=last_uid,
            )
        except Exception:  # noqa: BLE001 - re-delivery is the safe fallback
            self.log.exception(
                "Alibaba IMAP (%s): could not save poll cursor at uid %s; "
                "those messages will be re-delivered next poll",
                self.region_key,
                last_uid,
            )

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

    def _fetch_new(self, cursor: dict | None) -> dict | None:
        """Fetch everything above the cursor, regardless of read state.

        ``BODY.PEEK[]`` is deliberate: it returns the full source *without*
        setting ``\\Seen``, so fetching a message leaves the mailbox exactly
        as the user left it. Nothing here consults ``\\Seen`` either — the
        cursor is the only definition of "already handled".

        Args:
            cursor (dict | None): The stored ``{"uid_validity", "last_uid"}``,
                or ``None`` if this mailbox has never been polled.

        Returns:
            dict | None: ``{"uid_validity", "floor", "messages",
                "bootstrapped"}`` where ``messages`` is a list of
                ``(uid, raw_bytes)`` in ascending UID order — ``raw_bytes`` is
                ``None`` for a UID that could not be downloaded after
                :data:`_MAX_UID_ATTEMPTS` tries. ``None`` when the mailbox
                could not be read at all (logged, never raised, so the loop
                continues).
        """
        client = None
        messages: list[tuple[int, bytes | None]] = []
        try:
            client = self._connect()
            uid_validity = self._uid_validity(client)

            bootstrapped = cursor is None or (
                cursor.get("uid_validity") != uid_validity
            )
            if cursor is not None and bootstrapped:
                # The server renumbered the mailbox (restored or recreated
                # it), so every UID we stored now points at nothing.
                self.log.warning(
                    "Alibaba IMAP (%s): UIDVALIDITY changed (%s -> %s), "
                    "re-bootstrapping the poll cursor",
                    self.region_key,
                    cursor.get("uid_validity"),
                    uid_validity,
                )

            if bootstrapped:
                uids, floor = self._bootstrap_uids(client)
            else:
                floor = int(cursor.get("last_uid") or 0)
                uids = self._uids_after(client, floor)

            if len(uids) > _MAX_PER_POLL:
                self.log.info(
                    "Alibaba IMAP (%s): %d message(s) due, taking the oldest "
                    "%d this round",
                    self.region_key,
                    len(uids),
                    _MAX_PER_POLL,
                )
                uids = uids[:_MAX_PER_POLL]

            for uid in uids:
                status, payload = client.uid(
                    "FETCH", str(uid), "(BODY.PEEK[])"
                )
                if (
                    status != "OK"
                    or not payload
                    or not payload[0]
                    or not isinstance(payload[0], tuple)
                ):
                    self.log.warning(
                        "Alibaba IMAP (%s): FETCH failed for uid %s",
                        self.region_key,
                        uid,
                    )
                    if self._give_up(uid):
                        messages.append((uid, None))
                        continue
                    # Stop the batch here rather than skipping past it: the
                    # cursor must not advance over a message never read.
                    break
                messages.append((uid, payload[0][1]))
        except (imaplib.IMAP4.error, OSError) as exc:
            self.log.error(
                "Alibaba IMAP (%s) fetch failed: %s", self.region_key, exc
            )
            return None
        finally:
            self._close(client)
        return {
            "uid_validity": uid_validity,
            "floor": floor,
            "messages": messages,
            "bootstrapped": bootstrapped,
        }

    def _uid_validity(self, client: imaplib.IMAP4_SSL) -> str:
        """Read the selected mailbox's ``UIDVALIDITY`` generation token.

        Args:
            client (imaplib.IMAP4_SSL): A client with the mailbox selected.

        Returns:
            str: The generation token, or ``""`` if the server did not report
                one — in which case UID-renumbering simply can't be detected
                and the cursor is trusted as-is (still better than ``\\Seen``).
        """
        _, data = client.response("UIDVALIDITY")
        if data and data[0]:
            return data[0].decode("ascii", "replace").strip()
        self.log.warning(
            "Alibaba IMAP (%s): server reported no UIDVALIDITY for %s",
            self.region_key,
            self.mailbox,
        )
        return ""

    def _uids_after(
        self, client: imaplib.IMAP4_SSL, floor: int
    ) -> list[int]:
        """List UIDs above ``floor``, ascending.

        Args:
            client (imaplib.IMAP4_SSL): A client with the mailbox selected.
            floor (int): Highest UID already processed.

        Returns:
            list[int]: The UIDs still to process, oldest first.
        """
        status, data = client.uid("SEARCH", None, f"UID {floor + 1}:*")
        if status != "OK":
            self.log.error(
                "Alibaba IMAP (%s): SEARCH returned %s",
                self.region_key,
                status,
            )
            return []
        # RFC 3501: `n:*` matches the mailbox's highest UID even when that is
        # below n, so an empty mailbox tail comes back as one stale hit.
        return [uid for uid in self._parse_uids(data) if uid > floor]

    def _bootstrap_uids(
        self, client: imaplib.IMAP4_SSL
    ) -> tuple[list[int], int]:
        """Choose a starting point for a mailbox with no usable cursor.

        Backfilling the last ``ALIBABA_IMAP_BACKFILL_DAYS`` days rather than
        the whole mailbox keeps a first deployment (or a restored mailbox)
        from replaying years of unrelated mail into ``unmatched_emails``.
        Anything already ingested is absorbed by the duplicate guards, so
        overlapping the window is harmless.

        Args:
            client (imaplib.IMAP4_SSL): A client with the mailbox selected.

        Returns:
            tuple[list[int], int]: The UIDs to process now, and the cursor
                floor to record for them (one below the oldest, so a failure
                inside the batch is still retried).
        """
        days = max(0, int(self.settings.alibaba_imap_backfill_days))
        highest = max(self._parse_uids_from(client, "ALL"), default=0)
        if days == 0:
            # Explicitly "start from now": ignore the existing mailbox.
            self.log.info(
                "Alibaba IMAP (%s): no cursor and backfill disabled, "
                "starting from uid %s",
                self.region_key,
                highest,
            )
            return [], highest

        since = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).strftime("%d-%b-%Y")
        uids = self._parse_uids_from(client, "SINCE", since)
        if not uids:
            self.log.info(
                "Alibaba IMAP (%s): no cursor and nothing in the last %d "
                "day(s), starting from uid %s",
                self.region_key,
                days,
                highest,
            )
            return [], highest

        floor = min(uids) - 1
        self.log.info(
            "Alibaba IMAP (%s): no usable cursor, backfilling %d message(s) "
            "from the last %d day(s) (uid > %s)",
            self.region_key,
            len(uids),
            days,
            floor,
        )
        return uids, floor

    def _parse_uids_from(
        self, client: imaplib.IMAP4_SSL, *criteria: str
    ) -> list[int]:
        """Run a UID SEARCH and return its result as ascending ints.

        Args:
            client (imaplib.IMAP4_SSL): A client with the mailbox selected.
            *criteria (str): IMAP search criteria, e.g. ``("SINCE", "…")``.

        Returns:
            list[int]: Matching UIDs, ascending; empty if the search failed.
        """
        status, data = client.uid("SEARCH", None, *criteria)
        if status != "OK":
            self.log.error(
                "Alibaba IMAP (%s): SEARCH %s returned %s",
                self.region_key,
                " ".join(criteria),
                status,
            )
            return []
        return self._parse_uids(data)

    @staticmethod
    def _parse_uids(data) -> list[int]:
        """Turn a SEARCH response into a sorted list of UIDs.

        Args:
            data: The raw ``imaplib`` SEARCH payload.

        Returns:
            list[int]: Parsed UIDs, ascending. Non-numeric tokens are
                ignored — the server's own ordering is never relied on.
        """
        if not data or not data[0]:
            return []
        uids: list[int] = []
        for token in data[0].split():
            try:
                uids.append(int(token))
            except ValueError:
                continue
        return sorted(uids)

    def _mark_processed(self, uid: int) -> None:
        """Flag a handled message ``\\Seen``, optionally archiving it.

        Called after :meth:`process_inbound` succeeded. This is housekeeping,
        not bookkeeping: the persisted cursor is what decides whether a
        message is due, so a failure here costs nothing but a stray unread
        message in the mailbox. It stays because it keeps the mailbox
        readable for humans and drives the optional move into
        ``ALIBABA_IMAP_PROCESSED_MAILBOX`` (which is safe for the cursor —
        UIDs of the remaining messages never shift).

        Args:
            uid (int): The message UID to mark.

        Returns:
            None
        """
        client = None
        try:
            client = self._connect()
            if self.processed_mailbox:
                client.uid("COPY", str(uid), self.processed_mailbox)
                client.uid("STORE", str(uid), "+FLAGS", r"(\Deleted)")
                client.expunge()
            else:
                client.uid("STORE", str(uid), "+FLAGS", r"(\Seen)")
        except (imaplib.IMAP4.error, OSError) as exc:
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
    """Build one poller per configured Alibaba server.

    A server is included only when it has a mailbox address *and* a password
    configured, so a deployment that uses just one server doesn't start a
    second poller that would fail on every tick.

    Servers are deduplicated by ``(address, mailbox)`` and deliberately *not*
    by host: ``imap.sg.aliyun.com`` and ``imap.hk.aliyun.com`` are
    server-specific access points to the same Alibaba mailbox, so with the
    default configuration — where the ``ALIBABA_HK_*`` credentials fall back
    to the Singapore ones — two pollers would otherwise race over the same
    messages and both try to flag them. One mailbox, one poller.

    Server-specific polling flags (ALIBABA_IMAP_POLLING_ENABLED for Singapore
    and ALIBABA_HK_IMAP_POLLING_ENABLED for Hong Kong) take precedence over
    the global ALIBABA_INBOUND_ENABLED flag. When a server-specific flag is
    set, it controls whether a poller is created for that server.

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
            "polling_enabled_env": settings.alibaba_imap_polling_enabled,
        },
        {
            "region_key": "alibaba_hk",
            "host": settings.alibaba_hk_imap_host,
            "port": settings.alibaba_hk_imap_port,
            "address": settings.alibaba_hk_mail_address,
            "password": settings.alibaba_hk_mail_password,
            "mailbox": settings.alibaba_hk_imap_mailbox,
            "processed_mailbox": settings.alibaba_hk_imap_processed_mailbox,
            "polling_enabled_env": settings.alibaba_hk_imap_polling_enabled,
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

        # Check server-specific polling flag, fall back to global flag
        polling_enabled_env = region.pop("polling_enabled_env")
        if polling_enabled_env:
            polling_enabled = polling_enabled_env == "true"
        else:
            polling_enabled = settings.alibaba_inbound_enabled

        if not polling_enabled:
            logger.info(
                "Alibaba IMAP poller for %s disabled by configuration",
                region["region_key"],
            )
            continue

        identity = (region["address"].strip().lower(), region["mailbox"])
        if identity in seen:
            logger.info(
                "Alibaba IMAP poller for %s polls the same mailbox (%s/%s) as "
                "an already-configured server, skipping the duplicate",
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
