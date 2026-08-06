"""Inbound webhook parser for SendCloud (AuroraSendCloud).

SendCloud's inbound-mail webhook payload format has not been officially
documented in this repo yet (only the outbound *Basic Send* API has). This
parser follows the same field-naming convention as SendGrid's Inbound Parse
webhook (``from``/``to``/``subject``/``text``/``html``/``spam_score``/
``SPF``/``dkim``/``attachments``/``attachment-info``) as a best-effort
default. Rename the field keys below once SendCloud's real inbound payload
shape is confirmed against docs.aurorasendcloud.com.

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
import httpx  # Used if you choose to download via raw_message_url

from fastapi import Request

from src.webhook_factory.webhook_master import (
    InboundEmail,
    RawAttachment,
    WebhookParseError,
    WebhookParserMaster,
)


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

        Field names follow the SendGrid Inbound Parse convention — rename
        them if SendCloud's docs (see "Inbound Parse Webhook" section of
        docs.aurorasendcloud.com) specify different keys.

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
            headers = self._headers(form.get("headers"), msg)
            data = {
                "from": form.get("from", ""),
                "to": form.get("to", ""),
                "subject": form.get("subject")
                or headers.get("subject", ""),
                "text": form.get("text", ""),
                "html": form.get("html", ""),
                "spam_score": form.get("spam_score", "0") or "0",
                "dkim": form.get("dkim", ""),
                "spf": form.get("SPF", ""),
                "headers": headers,
                "raw_message": raw_eml,
                "attachments": self.attachments_from_mime(msg),
            }
        except Exception as exc:  # noqa: BLE001 - normalise to one type
            self.log.error("Failed to extract SendCloud multipart fields: %s", exc)
            raise WebhookParseError(
                f"Could not extract SendCloud multipart fields: {exc}"
            ) from exc
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
            data = {
                "from": payload.get("from") or payload.get("sender", ""),
                "to": payload.get("to") or payload.get("recipient", ""),
                "subject": payload.get("subject", ""),
                "text": payload.get("text") or payload.get("body_plain", ""),
                "html": payload.get("html") or payload.get("body_html", ""),
                "spam_score": payload.get("spam_score", 0),
                "dkim": payload.get("dkim", ""),
                "spf": payload.get("SPF") or payload.get("spf", ""),
                "headers": self._headers(payload.get("headers"), None),
                "raw_message": payload.get("raw_message") or "",
                "attachments": self._attachments_from_json(
                    payload.get("attachments", [])
                ),
            }
        except Exception as exc:  # noqa: BLE001 - normalise to one type
            self.log.error("Failed to extract SendCloud JSON fields: %s", exc)
            raise WebhookParseError(
                f"Could not extract SendCloud JSON payload: {exc}"
            ) from exc
        return data

    async def parse(self, request: Request) -> InboundEmail:
        """Convert a SendCloud inbound POST into a normalised inbound email.

        Dispatches to :meth:`extract_from_multipart` or
        :meth:`extract_from_json` based on the request's content type, then
        binds the extracted fields onto an :class:`InboundEmail`.

        Args:
            request (Request): The FastAPI request for the inbound POST.

        Returns:
            InboundEmail: The normalised inbound email with
                ``provider="sendcloud"``.

        Raises:
            WebhookParseError: If the content type is unsupported or the
                body cannot be read/extracted.
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
            self.log.error(
                "Unsupported content type for SendCloud inbound: %s",
                content_type,
            )
            raise WebhookParseError(
                f"Unsupported content type for SendCloud inbound: {content_type}"
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
            "Parsed SendCloud inbound: %s -> %s (%d attachment(s), "
            "message_id=%s)",
            inbound.from_email,
            inbound.to_email,
            len(inbound.attachments),
            inbound.message_id or "-",
        )
        return inbound

    async def _raw_message(self, form) -> str:
        """Return the inbound message's raw MIME source, downloading if needed.

        SendCloud inlines it in ``raw_message`` when small enough, otherwise
        exposes ``raw_message_url``. A failed download returns ``""`` rather
        than raising — without the headers, matching falls back to the
        subject token instead of losing the reply.

        Args:
            form: The parsed multipart form mapping.

        Returns:
            str: The raw ``.eml`` source, or ``""`` if unavailable.
        """
        raw_eml = form.get("raw_message")
        raw_url = form.get("raw_message_url")

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

    def _headers(self, raw_headers, msg) -> dict[str, str]:
        """Build the lowercase header dict from the payload and/or raw MIME.

        SendCloud's inbound payload shape is unconfirmed, so ``headers`` may
        arrive as a JSON string, an object, or not at all — whatever is
        present is merged, then overridden by the real MIME headers when a
        raw source was available.

        Args:
            raw_headers: The payload's ``headers`` value, if any.
            msg: The parsed MIME message, or ``None``.

        Returns:
            dict[str, str]: Lowercase header name → value.
        """
        headers: dict[str, str] = {}
        if isinstance(raw_headers, str) and raw_headers.strip():
            try:
                raw_headers = json.loads(raw_headers)
            except ValueError:
                # Not JSON — most likely a raw "Name: Value" header block.
                parsed = email.message_from_string(raw_headers, policy=default)
                headers.update(self.headers_from_mime(parsed))
                raw_headers = None
        if isinstance(raw_headers, dict):
            headers.update(
                {str(k).lower(): str(v) for k, v in raw_headers.items()}
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
