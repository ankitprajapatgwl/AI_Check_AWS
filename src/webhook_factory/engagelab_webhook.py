"""Inbound webhook parser for EngageLab.

EngageLab's Inbound Route posts a supplier reply to our
``POST /webhooks/rfq/inbound`` endpoint once the MX record for
``ENGAGELAB_OUTBOUND_DOMAIN`` points at EngageLab and a webhook is bound to
the sending API_USER.

The setup guide (``setup_docs/engagelab_guide/engagelab_setup.md``,
section 6) originally assumed a flat ``sender``/``recipient``/``message``
shape, but a real captured payload
(``setup_docs/engagelab_guide/webhook_response.json``) shows EngageLab
actually posts a nested envelope:

.. code-block:: text

    {
        "server": "email",
        "message_id": "...",
        "to": "OliverBennett-8ddfd168 <OliverBennett-8ddfd168@mail.jobsetu.online>",
        "itime": 1783401717715,
        "response": {
            "event": "route",
            "response_data": {
                "email_id": "...",
                "headers": {...raw MIME headers...},
                "raw_message": "...full raw MIME source...",
                "raw_message_url": "https://.../MXBODY.eml",
                "subject": "...",
                "from": "supplier@example.com",
                "from_name": "Supplier Name",
                "text": "...",
                "html": "...",
                "reference": "...",
                "x_mx_rcptto": "our-dynamic-address@mail.jobsetu.online",
                "x_mx_mailfrom": "supplier@example.com",
                "label_id": 0
            }
        }
    }

Notably: the supplier's address (``from``), the subject and both bodies
live under ``response.response_data`` — not at the top level — and there
is no dedicated ``attachments`` array. Attachments have to be recovered by
parsing ``response_data.raw_message`` (or downloading
``response_data.raw_message_url`` when the inline copy is absent) as a
MIME message. The flat guessed keys from the setup guide are kept as
fallbacks in case a different EngageLab event type uses that shape.

Example:
    >>> from src.webhook_factory.engagelab_webhook import (
    ...     EngageLabWebhookParser)
    >>> parser = EngageLabWebhookParser(settings, logger)  # doctest: +SKIP
    >>> inbound = await parser.parse(request)              # doctest: +SKIP
"""

import base64
import email
import json
from email.policy import default as email_default_policy
from email.utils import parseaddr

import httpx
from fastapi import Request

from src.webhook_factory.webhook_master import (
    InboundEmail,
    RawAttachment,
    WebhookParseError,
    WebhookParserMaster,
)


class EngageLabWebhookParser(WebhookParserMaster):
    """Parse EngageLab inbound payloads (JSON or ``multipart``/urlencoded
    form).

    Inherits attachment persistence and the default (trusted) signature
    check from :class:`WebhookParserMaster` — EngageLab's inbound route is
    secured by an unguessable URL bound to a single API_USER, not by an
    HMAC signature.

    Example:
        >>> parser = EngageLabWebhookParser(settings, logger)
        >>> parser.provider_name
        'engagelab'
    """

    @property
    def provider_name(self) -> str:
        """Return the provider key ``"engagelab"``.

        Returns:
            str: Always ``"engagelab"``.
        """
        return "engagelab"

    async def parse(self, request: Request) -> InboundEmail:
        """Convert an EngageLab inbound POST into a normalised inbound email.

        Dispatches on content type — EngageLab may post JSON or a form —
        then binds the extracted fields onto an :class:`InboundEmail`.

        Args:
            request (Request): The FastAPI request for the inbound POST.

        Returns:
            InboundEmail: The normalised inbound email with
                ``provider="engagelab"``.

        Raises:
            WebhookParseError: If the content type is unsupported or the
                body cannot be read/extracted.
        """
        content_type = request.headers.get("content-type", "")

        if "application/json" in content_type:
            try:
                payload = await request.json()
            except Exception as exc:  # noqa: BLE001 - normalise to one type
                self.log.error("Could not read EngageLab JSON body: %s", exc)
                raise WebhookParseError(
                    f"Could not read EngageLab JSON body: {exc}"
                ) from exc
            parsed = await self._extract(payload or {})
        elif (
            "multipart/form-data" in content_type
            or "application/x-www-form-urlencoded" in content_type
        ):
            try:
                form = await request.form()
            except Exception as exc:  # noqa: BLE001 - normalise to one type
                self.log.error("Could not read EngageLab form body: %s", exc)
                raise WebhookParseError(
                    f"Could not read EngageLab form body: {exc}"
                ) from exc
            parsed = await self._extract(form)
        else:
            self.log.error(
                "Unsupported content type for EngageLab inbound: %s",
                content_type,
            )
            raise WebhookParseError(
                f"Unsupported content type for EngageLab inbound: "
                f"{content_type}"
            )

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
            "Parsed EngageLab inbound: %s -> %s (%d attachment(s), "
            "message_id=%s)",
            inbound.from_email,
            inbound.to_email,
            len(inbound.attachments),
            inbound.message_id or "-",
        )
        return inbound

    async def _extract(self, payload) -> dict:
        """Extract common inbound email fields from a JSON or form payload.

        The real payload nests the fields that matter under
        ``response.response_data`` (see the module docstring); the flat
        ``sender``/``recipient``/``message`` keys from the original setup
        guide are kept as fallbacks in case another EngageLab event shape
        uses them.

        Args:
            payload: A ``dict`` (JSON body) or form mapping (``await
                request.form()``).

        Returns:
            dict: Normalised keys ``from``, ``to``, ``subject``, ``text``,
                ``html``, ``spam_score``, ``dkim``, ``spf``, ``headers``
                (lowercase-keyed), ``raw_message`` and ``attachments`` (a
                ``list[RawAttachment]``).

        Raises:
            WebhookParseError: If the fields cannot be read.
        """
        try:
            response_data = self._response_data(payload)
            payload_headers = response_data.get("headers") or {}

            # The raw MIME source is fetched once and parsed once: it is the
            # only reliable place to read the threading headers *and* the
            # only place attachments exist, so both come off the same object.
            raw_eml = await self._raw_message(response_data)
            msg = (
                email.message_from_string(raw_eml, policy=email_default_policy)
                if raw_eml
                else None
            )

            headers = {
                str(key).lower(): str(value)
                for key, value in (payload_headers or {}).items()
            }
            if msg is not None:
                # Headers off the real MIME source win over the payload's
                # summarised copy — that copy has been observed to omit
                # In-Reply-To entirely.
                headers.update(self.headers_from_mime(msg))

            from_addr = (
                response_data.get("from")
                or payload.get("sender")
                or payload.get("from", "")
            )
            to_addr = (
                payload.get("to")
                or response_data.get("x_mx_rcptto")
                or payload.get("recipient", "")
            )

            data = {
                "from": self._clean_address(from_addr),
                "to": self._clean_address(to_addr),
                "subject": response_data.get("subject")
                or payload.get("subject")
                or headers.get("subject", ""),
                "text": response_data.get("text") or payload.get("text", ""),
                "html": response_data.get("html") or payload.get("html", ""),
                "spam_score": payload.get("spam_score", 0) or 0,
                "dkim": response_data.get("dkim")
                or payload.get("dkim")
                or ("pass" if headers.get("dkim-signature") else ""),
                "spf": response_data.get("spf")
                or payload.get("spf")
                or headers.get("received-spf", ""),
                "headers": headers,
                "raw_message": raw_eml or "",
                "attachments": self._extract_attachments(
                    response_data, payload, msg
                ),
            }
        except Exception as exc:  # noqa: BLE001 - normalise to one type
            self.log.error("Failed to extract EngageLab fields: %s", exc)
            raise WebhookParseError(
                f"Could not extract EngageLab payload fields: {exc}"
            ) from exc
        return data

    @staticmethod
    def _response_data(payload) -> dict:
        """Unwrap ``response.response_data`` from a JSON or form payload.

        Some form-based providers send nested objects as a JSON-encoded
        string in a single field, so a string ``response`` value is decoded
        before unwrapping.

        Args:
            payload: A ``dict`` (JSON body) or form mapping.

        Returns:
            dict: The ``response_data`` object, or ``{}`` when absent.
        """
        response = payload.get("response", {})
        if isinstance(response, str):
            try:
                response = json.loads(response) if response else {}
            except ValueError:
                return {}
        return (response or {}).get("response_data") or {}

    @staticmethod
    def _clean_address(value: str) -> str:
        """Reduce a ``"Name <email>"`` value to the bare email address.

        EngageLab's top-level ``to`` field carries the full display form
        (e.g. ``"OliverBennett-8ddfd168 <OliverBennett-8ddfd168@...>"``); the
        conversation matcher only needs the address itself.

        Args:
            value (str): A raw address, with or without a display name.

        Returns:
            str: The bare email address, or ``value`` unchanged if it does
                not look like an address at all.
        """
        if not value:
            return ""
        _, addr = parseaddr(value)
        return addr or value

    async def _raw_message(self, response_data: dict) -> str:
        """Return the inbound message's raw MIME source, downloading if needed.

        EngageLab inlines the source in ``raw_message`` most of the time, but
        falls back to a ``raw_message_url`` when it is large. A failed
        download is logged and returns ``""`` rather than raising: losing the
        headers degrades matching to the subject token, which is far better
        than dropping the whole reply.

        Args:
            response_data (dict): The unwrapped ``response.response_data``.

        Returns:
            str: The raw ``.eml`` source, or ``""`` if unavailable.
        """
        raw_eml = response_data.get("raw_message")
        raw_url = response_data.get("raw_message_url")

        if not raw_eml and raw_url:
            self.log.debug(
                "EngageLab raw_message blank, downloading from "
                "raw_message_url..."
            )
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(raw_url, timeout=10.0)
                if response.status_code == 200:
                    raw_eml = response.text
                else:
                    self.log.error(
                        "EngageLab raw_message_url returned status %d",
                        response.status_code,
                    )
            except httpx.HTTPError as exc:
                self.log.error(
                    "Could not download EngageLab raw_message_url %s: %s",
                    raw_url,
                    exc,
                )
        return raw_eml or ""

    def _extract_attachments(
        self, response_data: dict, payload, msg
    ) -> list[RawAttachment]:
        """Recover attachments from an EngageLab inbound payload.

        EngageLab does not post a structured ``attachments`` array; the only
        real source is the raw MIME message, already parsed by the caller.
        An explicit ``attachments`` array is still checked first, for the
        setup guide's original guessed shape.

        Args:
            response_data (dict): The unwrapped ``response.response_data``
                object.
            payload: The full JSON/form payload, checked for a top-level
                ``attachments`` fallback.
            msg: The parsed MIME message, or ``None`` when no raw source was
                available.

        Returns:
            list[RawAttachment]: One entry per decodable attachment; empty
                when the message carried none or none could be recovered.
        """
        explicit = response_data.get("attachments") or payload.get(
            "attachments"
        )
        if explicit:
            return self._parse_attachments(explicit)
        return self.attachments_from_mime(msg)

    def _parse_attachments(self, raw_attachments) -> list[RawAttachment]:
        """Decode base64 attachment entries from an explicit attachments array.

        Kept for the setup guide's original guessed shape, in case some
        EngageLab event type does post attachments this way instead of only
        via the raw MIME message. Accepts a few plausible key names
        (``filename``/``name``, ``content_type``/``type``, ``content``/
        ``data``) and skips entries it cannot make sense of, logging why.

        Args:
            raw_attachments: The ``attachments`` value from the payload —
                expected to be a list of dicts. A JSON-encoded string (some
                form-based providers send arrays as a JSON string) is
                decoded first.

        Returns:
            list[RawAttachment]: One entry per decodable attachment; empty
                when the message carried none or the shape is unrecognised.
        """
        if isinstance(raw_attachments, str):
            try:
                raw_attachments = json.loads(raw_attachments) if raw_attachments else []
            except ValueError:
                self.log.debug("EngageLab attachments field is not valid JSON")
                return []

        attachments: list[RawAttachment] = []
        for index, item in enumerate(raw_attachments or [], start=1):
            if not isinstance(item, dict):
                self.log.debug(
                    "Skipping EngageLab attachment %d: not an object", index
                )
                continue

            filename = (
                item.get("filename") or item.get("name") or f"attachment_{index}"
            )
            content_type = (
                item.get("content_type")
                or item.get("type")
                or "application/octet-stream"
            )
            encoded = item.get("content") or item.get("data")
            if not encoded:
                self.log.debug(
                    "EngageLab attachment %s has no content", filename
                )
                continue

            try:
                content = base64.b64decode(encoded)
            except (TypeError, ValueError) as exc:
                self.log.error(
                    "Could not base64-decode EngageLab attachment %s: %s",
                    filename,
                    exc,
                )
                continue
            attachments.append(RawAttachment(filename, content_type, content))
        return attachments
