"""Inbound webhook parser for SendCloud (AuroraSendCloud).

SendCloud's inbound-mail webhook payload format has not been officially
documented in this repo yet (only the outbound *Basic Send* API has), so
this parser is written to survive not knowing it:

- Every logical field is probed across all plausible spellings
  (:data:`_FIELD_ALIASES`) instead of one guessed key.
- Anything still missing is recovered from the raw MIME source
  (``raw_message``, or downloaded from ``raw_message_url``) — sender,
  recipient, subject, both bodies and attachments all come back from there.
- A payload that still yields nothing logs its real field names, so one
  live request is enough to finish the mapping.

That matters because a wrong key name is silent: it produces an
:class:`~src.webhook_factory.webhook_master.InboundEmail` with every field
blank, which looks exactly like receiving an empty request. Note also that a
SendCloud POST reaches this parser only because
``src.route._resolve_inbound_provider`` falls back to ``"sendcloud"`` —
SendCloud's payload carries no marker naming it, so it is identified by
elimination once the four detectable providers are ruled out.

Example:
    >>> from src.webhook_factory.sendcloud_webhook import (
    ...     SendCloudWebhookParser)
    >>> parser = SendCloudWebhookParser(settings, logger)  # doctest: +SKIP
    >>> inbound = await parser.parse(request)              # doctest: +SKIP
"""

import base64
import json
import email
from email.policy import default
from email.utils import parseaddr
from urllib.parse import parse_qsl
import httpx  # Used if you choose to download via raw_message_url

from fastapi import Request

from src.webhook_factory.webhook_master import (
    InboundEmail,
    RawAttachment,
    WebhookParseError,
    WebhookParserMaster,
)

# Field names, confirmed against a real AuroraSendCloud Inbound Route POST
# captured 2026-08-07 (application/x-www-form-urlencoded, 22 fields):
#
#   event, emailId, reference, labelId, labelName, timestamp,
#   signature, token, message,
#   from, fromname, to, toname, subject, text, html,
#   headers, userHeaders, raw_message, raw_message_url,
#   x_mx_rcptto, x_mx_mailfrom
#
# Confirmed names come first in each tuple; the rest are kept as tolerated
# spellings in case a different event type or region varies. Fields probed
# per-alias rather than by one hard-coded key because a wrong key name here
# is silent — it yields an InboundEmail with every field blank, which is
# indistinguishable from receiving an empty request.
#
# Deliberately NOT mapped, despite looking useful:
#   ``message``   — a status string ("mx route"), not the email body.
#   ``reference`` — SendCloud's own internal id, not the RFC References
#                   header. Mapping it would corrupt conversation matching.
#   ``signature``/``token``/``timestamp`` — SendCloud's webhook signature
#                   triple. Identical in name to Mailgun's, which is exactly
#                   why inbound routing must not sniff for it (see
#                   src/route.py).
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    # x_mx_mailfrom is the SMTP envelope sender; ``from`` is the header one.
    "from": ("from", "x_mx_mailfrom", "From", "from_email", "sender"),
    # x_mx_rcptto is the address SendCloud actually routed on, already bare;
    # ``to`` carries the display form ("Name <addr>"). Envelope first.
    "to": ("x_mx_rcptto", "to", "To", "to_email", "recipient"),
    "subject": ("subject", "Subject", "title"),
    "text": ("text", "plain", "body_text", "body-plain", "content"),
    "html": ("html", "body_html", "body-html", "htmlContent"),
    "headers": ("headers", "header_list", "message-headers", "header"),
    "user_headers": ("userHeaders", "user_headers"),
    "raw_message": ("raw_message", "email", "eml", "mime"),
    "raw_message_url": ("raw_message_url", "eml_url", "message_url"),
    "spam_score": ("spam_score", "spamScore", "spam-score"),
    "dkim": ("dkim", "DKIM", "dkim_result"),
    "spf": ("SPF", "spf", "spf_result"),
}


class SendCloudWebhookParser(WebhookParserMaster):
    """Parse SendCloud inbound payloads (``multipart/form-data``,
    ``application/x-www-form-urlencoded``, or JSON).

    Inherits attachment persistence and the default (trusted) signature
    check from :class:`WebhookParserMaster`.

    Example:
        >>> parser = SendCloudWebhookParser(settings, logger)
        >>> parser.provider_name
        'sendcloud'
    """

    @property
    def provider_name(self) -> str:
        """Return the provider key ``"sendcloud"``.

        Returns:
            str: Always ``"sendcloud"``.
        """
        return "sendcloud"

    async def extract_from_multipart(self, form) -> dict:
        """Extract common inbound email fields from a multipart form.

        SendCloud's inbound field names are not documented, so every field
        is looked up across the plausible aliases in :data:`_FIELD_ALIASES`
        rather than one guessed key, and anything still missing is recovered
        from the raw MIME source. Reading a single guessed key was what made
        an arriving SendCloud POST parse to a completely blank email.

        Args:
            form: The parsed multipart form mapping (as returned by
                ``await request.form()``).

        Returns:
            dict: Normalised keys ``from``, ``to``, ``subject``, ``text``,
                ``html``, ``spam_score``, ``dkim``, ``spf`` and
                ``attachments`` (a ``list[RawAttachment]``).

        Raises:
            WebhookParseError: If the form fields cannot be read.
        """
        try:
            raw_eml = await self._raw_message(form)
            msg = (
                email.message_from_string(raw_eml, policy=default)
                if raw_eml
                else None
            )
            headers = self._headers(
                self._first(form, "headers"),
                msg,
                self._first(form, "user_headers"),
            )
            text, html = self._bodies_from_mime(msg)
            data = {
                "from": self._clean_address(
                    self._first(form, "from") or headers.get("from", "")
                ),
                "to": self._clean_address(
                    self._first(form, "to") or headers.get("to", "")
                ),
                "subject": self._first(form, "subject")
                or headers.get("subject", ""),
                "text": self._first(form, "text") or text,
                "html": self._first(form, "html") or html,
                "spam_score": self._first(form, "spam_score") or "0",
                "dkim": self._first(form, "dkim"),
                "spf": self._first(form, "spf"),
                "headers": headers,
                "raw_message": raw_eml,
                "attachments": self.attachments_from_mime(msg),
            }
        except Exception as exc:  # noqa: BLE001 - normalise to one type
            self.log.error("Failed to extract SendCloud multipart fields: %s", exc)
            raise WebhookParseError(
                f"Could not extract SendCloud multipart fields: {exc}"
            ) from exc
        self._warn_if_blank(data, sorted(form.keys()))
        return data

    async def extract_from_json(self, payload: dict) -> dict:
        """Extract common inbound email fields from a JSON payload.

        Adjust key names to match SendCloud's actual JSON schema once it is
        documented; the fallbacks below are a best-effort guess.

        Args:
            payload (dict): The parsed JSON body of the inbound request.

        Returns:
            dict: Normalised keys ``from``, ``to``, ``subject``, ``text``,
                ``html``, ``spam_score``, ``dkim``, ``spf`` and
                ``attachments`` (a ``list[RawAttachment]``).

        Raises:
            WebhookParseError: If the payload fields cannot be read.
        """
        try:
            raw_eml = await self._raw_message(payload)
            msg = (
                email.message_from_string(raw_eml, policy=default)
                if raw_eml
                else None
            )
            headers = self._headers(
                self._first(payload, "headers"),
                msg,
                self._first(payload, "user_headers"),
            )
            text, html = self._bodies_from_mime(msg)

            # An explicit attachments array wins; otherwise the raw MIME
            # source is the only place files can be recovered from.
            attachments = self._attachments_from_json(
                payload.get("attachments", [])
            ) or self.attachments_from_mime(msg)

            data = {
                "from": self._clean_address(
                    self._first(payload, "from") or headers.get("from", "")
                ),
                "to": self._clean_address(
                    self._first(payload, "to") or headers.get("to", "")
                ),
                "subject": self._first(payload, "subject")
                or headers.get("subject", ""),
                "text": self._first(payload, "text") or text,
                "html": self._first(payload, "html") or html,
                "spam_score": self._first(payload, "spam_score") or 0,
                "dkim": self._first(payload, "dkim"),
                "spf": self._first(payload, "spf"),
                "headers": headers,
                "raw_message": raw_eml,
                "attachments": attachments,
            }
        except Exception as exc:  # noqa: BLE001 - normalise to one type
            self.log.error("Failed to extract SendCloud JSON fields: %s", exc)
            raise WebhookParseError(
                f"Could not extract SendCloud JSON payload: {exc}"
            ) from exc
        self._warn_if_blank(data, sorted(payload.keys()))
        return data

    async def parse(self, request: Request) -> InboundEmail:
        """Convert a SendCloud inbound POST into a normalised inbound email.

        Dispatches to :meth:`extract_from_multipart` or
        :meth:`extract_from_json` based on the request's content type, then
        binds the extracted fields onto an :class:`InboundEmail`.

        An unrecognised content type is sniffed rather than rejected. The
        header is not something this app controls, SendCloud's is
        unconfirmed, and Starlette's ``request.form()`` returns an *empty*
        ``FormData`` — no error — for anything that is neither multipart nor
        urlencoded. Trusting the header therefore turns a mislabelled but
        perfectly readable body into a blank email, which is
        indistinguishable from receiving nothing at all.

        Args:
            request (Request): The FastAPI request for the inbound POST.

        Returns:
            InboundEmail: The normalised inbound email with
                ``provider="sendcloud"``.

        Raises:
            WebhookParseError: If the body cannot be read or extracted in
                any of the supported shapes.
        """
        content_type = request.headers.get("content-type", "")

        if (
            "multipart/form-data" in content_type
            or "application/x-www-form-urlencoded" in content_type
        ):
            try:
                form = await request.form()
            except Exception as exc:  # noqa: BLE001 - normalise to one type
                self.log.error("Could not read SendCloud form body: %s", exc)
                raise WebhookParseError(
                    f"Could not read SendCloud form body: {exc}"
                ) from exc
            parsed = await self.extract_from_multipart(form)
        elif "application/json" in content_type:
            try:
                payload = await request.json()
            except Exception as exc:  # noqa: BLE001 - normalise to one type
                self.log.error("Could not read SendCloud JSON body: %s", exc)
                raise WebhookParseError(
                    f"Could not read SendCloud JSON body: {exc}"
                ) from exc
            parsed = await self.extract_from_json(payload or {})
        else:
            parsed = await self._parse_unlabelled_body(request, content_type)

        try:
            spam_score = float(parsed.get("spam_score") or 0)
        except (TypeError, ValueError):
            spam_score = 0.0

        headers = parsed.get("headers", {})
        inbound = InboundEmail(
            from_email=parsed.get("from", ""),
            to_email=parsed.get("to", ""),
            subject=parsed.get("subject", ""),
            body_text=parsed.get("text", ""),
            body_html=parsed.get("html", ""),
            spam_score=spam_score,
            dkim=parsed.get("dkim", ""),
            spf=parsed.get("spf", ""),
            provider=self.provider_name,
            message_id=headers.get("message-id", ""),
            in_reply_to=headers.get("in-reply-to", ""),
            references=headers.get("references", ""),
            headers=headers,
            raw_message=parsed.get("raw_message", ""),
        )
        inbound.attachments = parsed.get("attachments", [])

        self.log.info(
            "Parsed SendCloud inbound: %s -> %s (%d attachment(s), "
            "message_id=%s)",
            inbound.from_email,
            inbound.to_email,
            len(inbound.attachments),
            inbound.message_id or "-",
        )
        return inbound

    async def _parse_unlabelled_body(
        self, request: Request, content_type: str
    ) -> dict:
        """Extract fields from a body whose content type says nothing useful.

        Tried in order: JSON, urlencoded form, then the body treated as raw
        MIME source. Only a body that is none of those is a real failure.

        Args:
            request (Request): The FastAPI request for the inbound POST.
            content_type (str): The (unrecognised) declared content type,
                for the log line.

        Returns:
            dict: The same normalised mapping the other extractors return.

        Raises:
            WebhookParseError: If the body is empty or matches no shape.
        """
        raw = await request.body()
        if not raw:
            raise WebhookParseError(
                "SendCloud inbound POST had an empty body "
                f"(content-type: {content_type or 'none'})"
            )

        self.log.warning(
            "Unrecognised content type for SendCloud inbound (%s) — sniffing "
            "the %d-byte body instead of rejecting it",
            content_type or "none",
            len(raw),
        )
        text = raw.decode("utf-8", errors="replace")

        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            self.log.info("SendCloud inbound body sniffed as JSON")
            return await self.extract_from_json(payload)

        # urlencoded bodies always carry '=' and no MIME header block.
        if "=" in text and not text.lstrip().lower().startswith(
            ("received:", "from:", "message-id:", "return-path:")
        ):
            form = dict(parse_qsl(text, keep_blank_values=True))
            if form:
                self.log.info(
                    "SendCloud inbound body sniffed as urlencoded form"
                )
                return await self.extract_from_multipart(form)

        if ":" in text:
            self.log.info("SendCloud inbound body sniffed as raw MIME")
            return await self.extract_from_multipart({"raw_message": text})

        raise WebhookParseError(
            f"Unsupported content type for SendCloud inbound: {content_type} "
            f"(body did not parse as JSON, form or MIME)"
        )

    def _first(self, payload, field: str) -> str:
        """Return the first non-empty value among a field's known aliases.

        SendCloud's inbound field names are unconfirmed, so each logical
        field is probed across every plausible spelling (see
        :data:`_FIELD_ALIASES`) instead of one guessed key. Uploaded files
        are ignored — a value is only useful here if it is text.

        Args:
            payload: A form mapping or JSON ``dict``.
            field (str): A key of :data:`_FIELD_ALIASES`.

        Returns:
            str: The first non-empty aliased value, or ``""``.
        """
        for alias in _FIELD_ALIASES[field]:
            value = payload.get(alias)
            if value is None or hasattr(value, "read"):
                continue
            if isinstance(value, (dict, list)):
                return value
            text = str(value).strip()
            if text:
                return text
        return ""

    @staticmethod
    def _clean_address(value) -> str:
        """Reduce a ``"Name <email>"`` value to the bare email address.

        The conversation matcher runs a dynamic-address regex over
        ``to_email``, and matching is by address, not display name.

        Args:
            value: A raw address, with or without a display name.

        Returns:
            str: The bare email address, or ``value`` unchanged when it does
                not look like an address at all.
        """
        if not value:
            return ""
        _, addr = parseaddr(str(value))
        return addr or str(value)

    @staticmethod
    def _bodies_from_mime(msg) -> tuple[str, str]:
        """Pull the plain-text and HTML bodies out of a parsed MIME message.

        The fallback for when SendCloud posts the raw message but no
        separate ``text``/``html`` fields — without it the reply is stored
        with an empty body even though its content arrived intact.

        Args:
            msg: An ``email.message.Message`` / ``EmailMessage``, or ``None``.

        Returns:
            tuple[str, str]: ``(text, html)``; either may be ``""``.
        """
        if msg is None:
            return "", ""
        text, html = "", ""
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            # A part with a filename is an attachment, not a body.
            if part.get_content_disposition() == "attachment" or part.get_filename():
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                decoded = payload.decode(charset, errors="replace")
            except LookupError:
                decoded = payload.decode("utf-8", errors="replace")
            subtype = part.get_content_subtype()
            if subtype == "plain" and not text:
                text = decoded
            elif subtype == "html" and not html:
                html = decoded
        return text, html

    def _warn_if_blank(self, data: dict, present_keys: list[str]) -> None:
        """Log the payload's real field names when nothing could be extracted.

        A SendCloud POST that yields no sender, no subject and no body means
        the field names below are still wrong. Printing what the payload
        actually contained turns that from a silent blank row into a
        one-line fix.

        Args:
            data (dict): The normalised fields just extracted.
            present_keys (list[str]): The keys the payload actually carried.

        Returns:
            None
        """
        if data.get("from") or data.get("subject") or data.get("text"):
            return
        self.log.warning(
            "SendCloud inbound parsed to an empty email — none of the "
            "expected field names matched. Payload carried: %s. Add the "
            "real names to _FIELD_ALIASES in "
            "src/webhook_factory/sendcloud_webhook.py.",
            ", ".join(present_keys) or "(no fields)",
        )

    async def _raw_message(self, form) -> str:
        """Return the inbound message's raw MIME source, downloading if needed.

        SendCloud inlines it in ``raw_message`` when small enough, otherwise
        exposes ``raw_message_url``. A failed download returns ``""`` rather
        than raising — without the headers, matching falls back to the
        subject token instead of losing the reply.

        Args:
            form: The parsed multipart form mapping, or a JSON ``dict``.

        Returns:
            str: The raw ``.eml`` source, or ``""`` if unavailable.
        """
        raw_eml = self._first(form, "raw_message")
        raw_url = self._first(form, "raw_message_url")

        if not raw_eml and raw_url:
            self.log.debug("raw_message blank, downloading from raw_message_url...")
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(raw_url, timeout=10.0)
                if response.status_code == 200:
                    raw_eml = response.text
                else:
                    self.log.error(
                        "SendCloud raw_message_url returned status %d",
                        response.status_code,
                    )
            except httpx.HTTPError as exc:
                self.log.error(
                    "Could not download SendCloud raw_message_url %s: %s",
                    raw_url,
                    exc,
                )

        if not raw_eml:
            self.log.debug("No raw message stream discovered in webhook payload.")
        return raw_eml or ""

    def _headers(self, raw_headers, msg, user_headers=None) -> dict[str, str]:
        """Build the lowercase header dict from the payload and/or raw MIME.

        SendCloud's ``headers`` field is **not** JSON and **not** an RFC
        header block: the live payload shows a Java ``Map.toString()`` dump
        (``{Name=value, Name=value}``, values themselves containing commas
        and newlines), which no parser can split unambiguously. It is
        therefore skipped rather than guessed at — ``raw_message`` carries
        the same headers losslessly and always wins anyway. ``userHeaders``
        (this app's own custom headers, echoed back) is real JSON and is
        merged when present, since that is where ``x-rfq-conversation-id``
        would come back.

        Args:
            raw_headers: The payload's ``headers`` value, if any.
            msg: The parsed MIME message, or ``None``.
            user_headers: The payload's ``userHeaders`` value, if any.

        Returns:
            dict[str, str]: Lowercase header name → value. MIME headers from
                ``raw_message`` override everything else.
        """
        headers: dict[str, str] = {}

        for source in (user_headers, raw_headers):
            if isinstance(source, str) and source.strip():
                try:
                    source = json.loads(source)
                except ValueError:
                    if source.lstrip().startswith("{"):
                        # Java Map.toString() — ambiguous, not parseable.
                        self.log.debug(
                            "Ignoring non-JSON SendCloud headers field "
                            "(%d chars); using raw_message headers instead",
                            len(source),
                        )
                        continue
                    # A genuine "Name: Value" block.
                    parsed = email.message_from_string(source, policy=default)
                    headers.update(self.headers_from_mime(parsed))
                    continue
            if isinstance(source, dict):
                headers.update(
                    {str(k).lower(): str(v) for k, v in source.items()}
                )

        if msg is not None:
            headers.update(self.headers_from_mime(msg))
        return headers

    def _attachments_from_json(self, raw_attachments) -> list[RawAttachment]:
        """Decode base64 attachment entries from a SendCloud JSON payload.

        The exact schema is unconfirmed, so this accepts a few plausible
        key names (``filename``/``content_type``/``type``/``content``/
        ``data``) and skips entries it cannot make sense of, logging why.

        Args:
            raw_attachments: The ``attachments`` value from the JSON
                payload — expected to be a list of dicts.

        Returns:
            list[RawAttachment]: One entry per decodable attachment; empty
                when the message carried none or the shape is unrecognised.
        """
        attachments: list[RawAttachment] = []
        for index, item in enumerate(raw_attachments or [], start=1):
            if not isinstance(item, dict):
                self.log.debug(
                    "Skipping SendCloud JSON attachment %d: not an object",
                    index,
                )
                continue

            filename = item.get("filename") or f"attachment_{index}"
            content_type = (
                item.get("content_type")
                or item.get("type")
                or "application/octet-stream"
            )
            encoded = item.get("content") or item.get("data")
            if not encoded:
                self.log.debug("SendCloud JSON attachment %s has no content", filename)
                continue

            try:
                content = base64.b64decode(encoded)
            except (TypeError, ValueError) as exc:
                self.log.error(
                    "Could not base64-decode SendCloud attachment %s: %s",
                    filename,
                    exc,
                )
                continue
            attachments.append(RawAttachment(filename, content_type, content))
        return attachments
