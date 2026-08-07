"""HTTP routes for EmailPOC — UI pages and the single inbound webhook.

This module contains **only** route declarations. Every handler is thin: it
reads request input, delegates to the shared
:class:`~src.services.conversation_service.ConversationService` (and the
Jinja2 templates) stored on ``request.app.state``, and returns a response.
All construction and wiring lives in :mod:`src.app`, so this file can be
read as a flat table of "URL → behaviour".

Every route below requires a logged-in user (see :mod:`src.auth`) except the
inbound webhook, which providers call anonymously.

Routes:

=============================== ==============================================
Method + path                   Purpose
=============================== ==============================================
``GET /``                       Redirect to ``/tracking``.
``GET /send``                   Render the Send RFQ form.
``POST /draft``                 Mint a conv_id + draft row, return its subject.
``POST /send``                  Send the RFQ (drafting first if needed).
``GET /tracking``               Personal dashboard: my stats + conversations.
``GET /tracking/{c}``           Full conversation thread (ownership-checked).
``POST /tracking/{c}/delete``   Delete one of my conversations.
``POST /webhooks/rfq/inbound``  Receive an inbound reply from any provider;
                                which one is resolved from the request (see
                                :func:`_resolve_inbound_provider`).
``GET /webhooks/rfq/inbound``   Validation probe (Elastic Email GETs this).
=============================== ==============================================

Example:
    >>> from src.route import router
    >>> from fastapi import FastAPI
    >>> app = FastAPI()
    >>> app.include_router(router)            # doctest: +SKIP
"""

import json
from typing import List
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse

from src.auth.dependencies import require_login
from src.config import BASE_PATH
from src.email_platform.email_master import EmailProviderError
from src.services.conversation_service import ConversationService

# A single router that :mod:`src.app` includes on the FastAPI application.
router = APIRouter()

# Human-friendly labels for providers surfaced anywhere in the UI, keyed by
# the same provider key used in src/email_platform/factory.py.
_PROVIDER_LABELS = {
    "engagelab": "EngageLab",
    "sendcloud": "SendCloud",
    "sendgrid": "SendGrid",
    "mailgun": "Mailgun",
    "elasticemail": "Elastic Email",
    "alibaba": "Alibaba Enterprise",
}

# Provider keys offered on the Send RFQ form's "Provider" dropdown and as
# Quick Send cards, in display order.
_FORM_PROVIDERS = ["sendcloud", "engagelab", "alibaba"]

_SUPPLIER_TYPE_LABELS = {"chinese": "Chinese", "non_chinese": "Non-Chinese"}

# Which supplier types each provider on the form can actually be used for,
# and which internal factory key (see src/email_platform/factory.py) that
# combination resolves to.
#
# SendCloud reaches Chinese *and* non-Chinese recipients through different
# servers: its Hong Kong/CN and Singapore base URLs are region-locked to
# separate credentials (see
# setup_docs/aurora_send_cloud/AuroraSendCloud_Documentation.md §2), so the
# "chinese" supplier type resolves to the separate "_hk" factory key.
# EngageLab's two data centers (Singapore/Turkey, per
# setup_docs/engagelab_guide/Engagelab_Documentation.md §2) aren't documented
# as a China-vs-non-China split, so it sends through the same Singapore
# endpoint for both. SendGrid has no regional split and is reserved for
# Non-Chinese suppliers.
#
# Alibaba sends **everything through the Hong Kong server** — both supplier
# types resolve to "alibaba_hk". One endpoint means one mailbox, one SMTP
# route and one place for replies to arrive, and Hong Kong is the better
# positioned of the two for Chinese mailboxes while working fine for the rest.
# The Singapore endpoint ("alibaba", smtp.qiye.aliyun.com) stays registered in
# the factory — the HK provider subclasses it, historical conversations still
# carry send_key='alibaba', and pointing "non_chinese" back at it is a
# one-line change here if the split is ever wanted again.
_SEND_KEYS = {
    ("sendcloud", "chinese"): "sendcloud_hk",
    ("sendcloud", "non_chinese"): "sendcloud",
    ("engagelab", "chinese"): "engagelab",
    ("engagelab", "non_chinese"): "engagelab",
    ("sendgrid", "non_chinese"): "sendgrid",
    ("alibaba", "chinese"): "alibaba_hk",
    ("alibaba", "non_chinese"): "alibaba_hk",
}

# Which region each (provider, supplier type) pair actually sends through,
# shown as a hint on the Quick Send cards so a tester knows what they're
# exercising without reading _SEND_KEYS. "alibaba" is kept for the historical
# rows that recorded it as their send_key, even though nothing routes there
# now.
_SEND_KEY_HINTS = {
    "sendcloud": "Hong Kong/CN region",
    "sendcloud_hk": "Hong Kong/CN region",
    "engagelab": "Singapore region",
    "sendgrid": "Global",
    "alibaba": "Alibaba Singapore server",
    "alibaba_hk": "Alibaba Hong Kong server",
}

# ── Inbound webhook URL ──────────────────────────────────────────────
#
# ONE inbound URL, with no provider segment in it. No email provider
# identifies itself on an inbound POST: EngageLab and SendCloud/Aurora both
# simply re-post the reply to whatever URL string was typed into their
# dashboard's WebHook / Inbound Route field, with no provider name, header
# or body field of their own, and neither dashboard can append a path
# segment (SendGrid, Mailgun and Elastic Email behave the same way; Alibaba
# has no webhook at all and is polled over IMAP instead). A
# ``/{provider_key}`` route would therefore only ever 404 — see
# setup_docs/engagelab_guide/Engagelab_Documentation.md §6 and
# setup_docs/aurora_send_cloud/AuroraSendCloud_Documentation.md §8, which
# both configure exactly this path.


# Body markers that name the provider that posted, checked by
# _resolve_inbound_provider. Only unambiguous ones are listed: SendGrid and
# SendCloud are deliberately absent because SendCloud's real payload shape
# is undocumented and this repo's parser mirrors SendGrid's field names
# exactly (see src/webhook_factory/sendcloud_webhook.py), so nothing in the
# body tells those two apart — they need the optional ?provider= override.
_MAILGUN_SIGNATURE_KEYS = ("signature", "token", "timestamp")
_MAILGUN_BODY_KEYS = ("body-plain", "body-html", "message-headers")
_ELASTICEMAIL_KEYS = ("from_email", "body_text", "header_list")

# Last-resort parser for a POST that named no provider and matched no
# marker. EngageLab is the default because its Inbound Route is the one
# pointed at the bare URL today.
_DEFAULT_INBOUND_PROVIDER = "engagelab"


def _resolve_send_key(provider_name: str, supplier_type: str) -> str:
    """Validate a provider/supplier-type pair and resolve its factory key.

    Shared by ``POST /draft`` and ``POST /send`` so both reject the same
    combinations with the same messages.

    Args:
        provider_name (str): The user-facing provider key from the form.
        supplier_type (str): ``"chinese"`` or ``"non_chinese"``.

    Returns:
        str: The resolved factory key, e.g. ``"alibaba_hk"``.

    Raises:
        EmailProviderError: If either value is missing/unknown, or the pair
            is one the provider doesn't support.
    """
    key = (provider_name or "").strip().lower()
    stype = (supplier_type or "").strip().lower()
    if not key:
        raise EmailProviderError("Please select an email provider.")
    if stype not in _SUPPLIER_TYPE_LABELS:
        raise EmailProviderError("Please select a supplier type.")
    if key not in _PROVIDER_LABELS:
        raise EmailProviderError(f"Unknown email provider '{provider_name}'.")

    send_key = _SEND_KEYS.get((key, stype))
    if send_key is None:
        raise EmailProviderError(
            f"{_PROVIDER_LABELS.get(key, key)} doesn't support "
            f"{_SUPPLIER_TYPE_LABELS[stype]} suppliers. Pick a provider "
            "that supports this supplier type."
        )
    return send_key


def _available_providers(settings) -> list[dict]:
    """List the providers offered on the Send RFQ form and Quick Send.

    Args:
        settings: The application :class:`~src.config.Settings`.

    Returns:
        list[dict]: One entry per provider in :data:`_FORM_PROVIDERS` with
            ``key``, ``label``, ``outbound_domain``, ``supported_types``,
            ``from_address_preview`` and ``region_hints``.
            ``from_address_preview`` drives the live "From" preview on the
            form — Alibaba shows its single authenticated mailbox, everyone
            else the ``{CamelName}@{domain}`` pattern. ``supported_types``
            lets the client warn on a combination the server would reject
            (the server re-validates regardless — see
            :func:`_resolve_send_key`). A provider whose domain isn't
            configured yet just shows an empty preview; the send itself still
            fails fast with a clear error.
    """
    providers = []
    for key in _FORM_PROVIDERS:
        supported = [stype for (pkey, stype) in _SEND_KEYS if pkey == key]
        providers.append({
            "key": key,
            "label": _PROVIDER_LABELS.get(key, key.capitalize()),
            "outbound_domain": settings.provider_outbound_domain(key),
            "supported_types": supported,
            # Alibaba SMTP only accepts its authenticated mailbox as From, so
            # its preview is a fixed address rather than a per-user pattern.
            "from_address_preview": (
                settings.alibaba_mail_address or ""
                if key == "alibaba"
                else ""
            ),
            "region_hints": {
                stype: _SEND_KEY_HINTS.get(
                    _SEND_KEYS[(key, stype)], _SEND_KEYS[(key, stype)]
                )
                for stype in supported
            },
        })
    return providers


# Maps ConversationService's inbound "status" field to an HTTP status code.
# "matched"/"unmatched"/"skipped"/"duplicate" are all valid, non-retryable
# outcomes (a spam email, an unrecognised sender, or a redelivery is not a
# delivery failure), so they stay 200 — a non-2xx would make most providers
# retry the same POST. "rejected" (bad signature) and "error" (unparseable
# payload) are genuine failures and get a non-2xx status.
_INBOUND_STATUS_CODES = {
    "matched": 200,
    "unmatched": 200,
    "skipped": 200,
    "duplicate": 200,
    "rejected": 400,
    "error": 500,
}


# ── UI routes ────────────────────────────────────────────────────────


@router.get("/")
async def home(_current_user: dict = Depends(require_login)):
    """Redirect the authenticated root path to the personal dashboard."""
    return RedirectResponse(f"{BASE_PATH}/tracking", status_code=303)


@router.get("/quick-send")
async def quick_send_page(
    request: Request,
    current_user: dict = Depends(require_login),
):
    """Render the Quick Test Send page.

    A one-click testing surface: one card per provider in
    :data:`_FORM_PROVIDERS` (SendCloud, EngageLab, Alibaba Enterprise), each
    split into a Chinese and a Non-Chinese section that names the region it
    resolves to. Every section only asks for the destination email — the
    rest of the RFQ payload (supplier name, product, quantity, target price)
    is filled in with fixed defaults client-side (see
    ``templates/quick_send.html``), which then posts to the same
    ``POST /send`` this module already exposes, so Quick Send exercises the
    identical draft-then-send path as the full form. On a successful send the
    page opens the resulting conversation's tracking page
    (``/tracking/{conv_id}``) in a new tab.

    Args:
        request (Request): FastAPI request (required by Jinja2).
        current_user (dict): The logged-in user (sender identity).

    Returns:
        TemplateResponse: The rendered ``quick_send.html`` template.
    """
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "quick_send.html",
        {
            "active_page": "quick_send",
            "current_user": current_user,
            "available_providers": _available_providers(request.app.state.settings),
        },
    )


@router.get("/send")
async def send_email_page(
    request: Request,
    success: str = "",
    error: str = "",
    conv_id: str = "",
    current_user: dict = Depends(require_login),
):
    """Render the Send RFQ form page.

    Displays the HTML form for composing a new RFQ. Optional query
    parameters let the page surface success/error feedback after a
    POST/redirect cycle. The form has a required "Provider" dropdown
    (SendCloud / EngageLab / Alibaba Enterprise, default empty) and the
    Supplier Details section has a required "Supplier Type" dropdown
    (Chinese / Non-Chinese, default empty). All three reach both supplier
    types; SendCloud routes Chinese suppliers through its Hong Kong region and
    everyone else through Singapore, Alibaba sends both through its Hong Kong
    server, and EngageLab uses the same Singapore endpoint either way.
    :func:`_resolve_send_key`
    resolves the provider+supplier-type pair to the actual sending region
    server-side (see :data:`_SEND_KEYS`) and rejects any combination a
    provider doesn't support.

    Args:
        request (Request): FastAPI request (required by Jinja2).
        success (str): Non-empty value triggers a success banner.
        error (str): Non-empty value triggers an error banner with the
            URL-decoded message.
        conv_id (str): Conversation id used in the success banner link.
        current_user (dict): The logged-in user (sender identity).

    Returns:
        TemplateResponse: The rendered ``index.html`` template.
    """
    templates = request.app.state.templates
    service: ConversationService = request.app.state.service
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "active_page": "send",
            "success": success,
            "error": error,
            "conv_id": conv_id,
            "current_user": current_user,
            "available_providers": _available_providers(request.app.state.settings),
            "predefined_projects": await service.db.get_predefined_projects(),
        },
    )


@router.post("/draft")
async def create_draft(
    request: Request,
    supplier_email: str = Form(...),
    supplier_name: str = Form(default=""),
    supplier_type: str = Form(...),
    provider_name: str = Form(...),
    product_name: str = Form(default=""),
    quantity: str = Form(default=""),
    target_price: str = Form(default=""),
    project_id: str = Form(default=""),
    project_name: str = Form(default=""),
    current_user: dict = Depends(require_login),
) -> JSONResponse:
    """Mint a conversation id and persist it as a draft (requirements 1+2).

    Called by the Send RFQ form before submitting, so the page can show the
    real reference and the exact subject that will go out. Nothing is sent
    here — the conversation lands in ``status='draft'``.

    Args:
        request (Request): FastAPI request.
        supplier_email (str): Supplier's email address.
        supplier_name (str): Supplier's display name.
        supplier_type (str): ``"chinese"`` or ``"non_chinese"``.
        provider_name (str): Provider key selected on the form.
        product_name (str): Product being quoted.
        quantity (str): Requested quantity in units.
        target_price (str): Target unit price, e.g. ``"$12.00"``.
        project_id (str): UUID of the selected catalog product, if any.
        project_name (str): That product's name.
        current_user (dict): The logged-in user (conversation owner).

    Returns:
        JSONResponse: ``{"conv_id": ..., "subject": "[RFQ - hd273hsd] - ..."}``
            on success, or ``{"error": ...}`` with ``400`` when the
            provider/supplier-type pair is invalid.
    """
    service: ConversationService = request.app.state.service
    log = request.app.state.log
    try:
        send_key = _resolve_send_key(provider_name, supplier_type)
        conversation = await service.create_draft(
            user_id=current_user["id"],
            supplier_email=supplier_email,
            supplier_name=supplier_name,
            supplier_type=supplier_type.strip().lower(),
            product_name=product_name,
            quantity=quantity or None,
            target_price=target_price,
            project_id=project_id,
            project_name=project_name,
            provider_name=provider_name.strip().lower(),
            send_key=send_key,
        )
        return JSONResponse({
            "conv_id": conversation["conv_id"],
            "subject": conversation["subject"],
        })
    except EmailProviderError as exc:
        log.error("Draft failed: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001 - last-resort safety net
        log.exception("Unexpected error while creating a draft")
        return JSONResponse({"error": str(exc)[:300]}, status_code=500)


@router.post("/send")
async def send_email_form(
    request: Request,
    conv_id: str = Form(default=""),
    project_id: str = Form(default=""),
    project_name: str = Form(default=""),
    provider_name: str = Form(default=""),
    supplier_type: str = Form(default=""),
    supplier_email: str = Form(...),
    supplier_name: str = Form(...),
    product_name: str = Form(...),
    quantity: int = Form(...),
    target_price: str = Form(...),
    attachments: List[UploadFile] = File(default=[]),
    current_user: dict = Depends(require_login),
):
    """Send an RFQ, drafting the conversation first if one wasn't made yet.

    Requirement 1's draft-then-send ordering always holds: if the form
    already created a draft (``conv_id`` present, owned by this user and
    still in ``draft``) it is reused, and otherwise one is created inline
    before sending. That keeps Quick Send — and a browser with JavaScript
    disabled — on exactly the same path as the full form.

    The RFQ goes out through the provider chosen on the required "Provider"
    dropdown, routed to the region matching the required "Supplier Type"
    (SendCloud/Alibaba: Hong Kong for Chinese, Singapore for Non-Chinese;
    see :data:`_SEND_KEYS`). An unsupported combination is rejected, as is
    an empty selection, in case the browser's own ``required`` validation
    was bypassed. Provider failures leave the conversation ``failed`` and
    are surfaced as a banner rather than a 500 page. The sender is always
    the logged-in user.

    Args:
        request (Request): FastAPI request.
        conv_id (str): Optional draft to send; created inline when absent.
        provider_name (str): Provider key selected on the form.
        supplier_type (str): ``"chinese"`` or ``"non_chinese"``.
        supplier_email (str): Supplier's email address.
        supplier_name (str): Supplier's display name.
        product_name (str): Name of the product being quoted.
        quantity (int): Requested quantity in units.
        target_price (str): Target unit price string, e.g. ``"$12.00"``.
        attachments (List[UploadFile]): Files to attach.
        current_user (dict): The logged-in user (sender identity).

    Returns:
        RedirectResponse: ``303`` to ``/tracking/{conv_id}`` on success, or
            back to ``/send?error=...`` on failure.
    """
    service: ConversationService = request.app.state.service
    log = request.app.state.log
    try:
        send_key = _resolve_send_key(provider_name, supplier_type)
        stype = supplier_type.strip().lower()

        attachment_data = []
        for upload in attachments:
            if upload.filename:
                content = await upload.read()
                attachment_data.append(
                    {
                        "filename": upload.filename,
                        "content": content,
                        "content_type": (
                            upload.content_type or "application/octet-stream"
                        ),
                    }
                )

        conv_id = await _resolve_draft(
            service,
            conv_id=conv_id.strip(),
            current_user=current_user,
            supplier_email=supplier_email,
            supplier_name=supplier_name,
            supplier_type=stype,
            product_name=product_name,
            quantity=quantity,
            target_price=target_price,
            project_id=project_id,
            project_name=project_name,
            provider_name=provider_name.strip().lower(),
            send_key=send_key,
        )

        await service.send_rfq(
            conv_id=conv_id,
            user_id=current_user["id"],
            provider_name=send_key,
            attachments=attachment_data or None,
        )
        return RedirectResponse(
            f"{BASE_PATH}/tracking/{conv_id}?success=1",
            status_code=303,
        )
    except EmailProviderError as exc:
        # Expected, well-described failure (bad key, send rejected, ...).
        log.error("Send failed: %s", exc)
        return RedirectResponse(
            f"{BASE_PATH}/send?error={quote(str(exc)[:300])}",
            status_code=303,
        )
    except Exception as exc:  # noqa: BLE001 - last-resort safety net
        log.exception("Unexpected error while sending RFQ")
        return RedirectResponse(
            f"{BASE_PATH}/send?error={quote(str(exc)[:300])}",
            status_code=303,
        )


async def _resolve_draft(
    service: ConversationService,
    *,
    conv_id: str,
    current_user: dict,
    **draft_fields,
) -> str:
    """Return the conv_id to send, creating a draft when there isn't one.

    A supplied ``conv_id`` is only reused when it exists, belongs to the
    current user and is still a draft — anything else (an unknown id, one
    guessed from another account, or a conversation already sent) falls back
    to minting a fresh draft rather than failing or, worse, re-sending on
    someone else's conversation.

    Args:
        service (ConversationService): The conversation service.
        conv_id (str): The candidate draft id from the form, possibly empty.
        current_user (dict): The logged-in user.
        **draft_fields: Passed straight to
            :meth:`~src.services.conversation_service.ConversationService.create_draft`.

    Returns:
        str: The conversation id to send.
    """
    if conv_id:
        existing = await service.db.get_conversation(conv_id)
        if (
            existing
            and str(existing["user_id"]) == str(current_user["id"])
            and existing["status"] == "draft"
        ):
            return conv_id
        service.log.info(
            "Ignoring conv_id %s on send (missing, not owned, or already "
            "sent) — creating a fresh draft",
            conv_id,
        )

    conversation = await service.create_draft(
        user_id=current_user["id"], **draft_fields
    )
    return conversation["conv_id"]


@router.get("/tracking")
async def tracking_home(
    request: Request,
    deleted: str = "",
    current_user: dict = Depends(require_login),
):
    """Render the personal dashboard: my stats + my conversations.

    Args:
        request (Request): FastAPI request.
        deleted (str): Non-empty value triggers a "conversation deleted"
            banner after a delete/redirect cycle.
        current_user (dict): The logged-in user.

    Returns:
        TemplateResponse: Rendered ``tracking.html`` with ``stats`` and
            ``conversations`` scoped to ``current_user``.
    """
    service: ConversationService = request.app.state.service
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "tracking.html",
        {
            "active_page": "tracking",
            "current_user": current_user,
            "stats": await service.db.get_user_stats(current_user["id"]),
            "conversations": await service.db.get_user_conversations(
                current_user["id"]
            ),
            "deleted": deleted,
        },
    )


@router.get("/tracking/{conv_id}")
async def conversation_detail(
    request: Request,
    conv_id: str,
    success: str = "",
    current_user: dict = Depends(require_login),
):
    """Render the full email thread for a single conversation.

    Merges sent and received records into one chronological timeline; each
    item carries a ``direction`` key so the template can style sent vs
    received differently.

    Args:
        request (Request): FastAPI request.
        conv_id (str): The 8-character conversation identifier (token).
        success (str): Non-empty triggers a success banner.
        current_user (dict): The logged-in user (used for the ownership
            check).

    Returns:
        TemplateResponse: Rendered ``conversation_detail.html`` with
            ``conversation`` and ``thread``.

    Raises:
        HTTPException: ``404`` if ``conv_id`` is not found or belongs to a
            different user.
    """
    service: ConversationService = request.app.state.service
    templates = request.app.state.templates

    conversation = await service.db.get_conversation(conv_id)
    if not conversation or str(conversation["user_id"]) != str(current_user["id"]):
        raise HTTPException(status_code=404, detail="Conversation not found")

    thread = []
    for email in conversation.get("emails_sent", []):
        thread.append(
            {
                **email,
                "direction": "sent",
                "_ts": email.get("sent_at", ""),
            }
        )
    for email in conversation.get("emails_received", []):
        thread.append(
            {
                **email,
                "direction": "received",
                "_ts": email.get("received_at", ""),
            }
        )
    thread.sort(key=lambda item: item.get("_ts", ""))

    return templates.TemplateResponse(
        request,
        "conversation_detail.html",
        {
            "active_page": "tracking",
            "current_user": current_user,
            "conversation": conversation,
            "thread": thread,
            "success": success,
        },
    )


@router.post("/tracking/{conv_id}/delete")
async def delete_conversation(
    request: Request, conv_id: str, current_user: dict = Depends(require_login)
):
    """Delete one of my conversations and its attachments, then redirect.

    Args:
        request (Request): FastAPI request.
        conv_id (str): The conversation token to delete.
        current_user (dict): The logged-in user (used for the ownership
            check).

    Returns:
        RedirectResponse: ``303`` to ``/tracking?deleted=1``.

    Raises:
        HTTPException: ``404`` if ``conv_id`` is not found or belongs to a
            different user.
    """
    service: ConversationService = request.app.state.service
    if not await service.delete_conversation(conv_id, current_user["id"]):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return RedirectResponse(f"{BASE_PATH}/tracking?deleted=1", status_code=303)


# ── Inbound webhook ──────────────────────────────────────────────────
async def _resolve_inbound_provider(request: Request) -> str:
    """Work out which provider posted an inbound reply.

    Needed because no provider names itself (see :data:`"/webhooks/rfq/inbound"`) —
    yet no single parser can decode every payload format, so
    :meth:`ConversationService.get_parser` still has to be given a key.
    Sources are tried in descending order of reliability:

    1. An optional ``?provider=`` (or ``?provider_key=``) query parameter.
       Nothing requires it — it is a manual override that wins over the
       guess below, for a payload no marker can identify.
    2. The payload's own shape — EngageLab's nested
       ``response.response_data`` envelope, Mailgun's signature triple or
       ``body-plain``/``body-html``, Elastic Email's
       ``from_email``/``body_text``/``header_list``.
    3. :data:`_DEFAULT_INBOUND_PROVIDER`.

    SendCloud and SendGrid are not detectable by shape (see
    :data:`_MAILGUN_SIGNATURE_KEYS` and the comment above it), so SendCloud
    is the one provider that needs the ``?provider=sendcloud`` override.

    The body is read here, before the parser runs. Starlette lets the
    request stream be consumed only once but caches it on the request, so
    the parser's later ``request.json()`` / ``request.form()`` call is
    served from that cache rather than an exhausted stream.

    Args:
        request (Request): The inbound POST.

    Returns:
        str: A provider key to hand to
            :meth:`ConversationService.get_parser`.
    """
    explicit = (
        request.query_params.get("provider")
        or request.query_params.get("provider_key")
        or ""
    ).strip().lower()
    if explicit:
        return explicit

    try:
        raw = await request.body()
        if not raw:
            return _DEFAULT_INBOUND_PROVIDER

        if "application/json" in request.headers.get("content-type", ""):
            payload = json.loads(raw)
            response = (
                payload.get("response") if isinstance(payload, dict) else None
            )
            if isinstance(response, dict) and (
                response.get("event") == "route" or "response_data" in response
            ):
                return "engagelab"
            return _DEFAULT_INBOUND_PROVIDER

        form = await request.form()
        if all(key in form for key in _MAILGUN_SIGNATURE_KEYS):
            return "mailgun"
        if any(key in form for key in _MAILGUN_BODY_KEYS):
            return "mailgun"
        if all(key in form for key in _ELASTICEMAIL_KEYS):
            return "elasticemail"
        if "response" in form:
            return "engagelab"
    except Exception:  # noqa: BLE001 - a failed guess must not fail the POST
        return _DEFAULT_INBOUND_PROVIDER

    return _DEFAULT_INBOUND_PROVIDER


@router.post("/webhooks/rfq/inbound")
async def handle_inbound_email(request: Request):
    """Receive and process one inbound email — the only inbound URL.

    This is the endpoint to enter in a provider dashboard::

        https://<host>/email_poc/webhooks/rfq/inbound

    There is no provider segment in the path, because no provider puts one
    there: EngageLab's WebHook field and SendCloud/Aurora's Inbound Route
    both POST to this exact string and nothing more. Which parser to use is
    worked out per request by :func:`_resolve_inbound_provider` and resolved
    through :meth:`ConversationService.get_parser`.

    Alibaba has no inbound webhook at all: its replies arrive through the
    IMAP poller (see :mod:`src.inbound.alibaba_imap_poller`) and feed the
    same pipeline.

    Parsing, conversation matching, attachment storage and reply
    classification all happen inside
    :meth:`ConversationService.handle_inbound`. Deliberately public —
    providers call this anonymously, so it is never behind ``require_login``.

    Args:
        request (Request): FastAPI request. The body is form or JSON data
            depending on the provider.

    Returns:
        JSONResponse: The status payload from
            :meth:`ConversationService.handle_inbound` — one of ``matched``,
            ``unmatched``, ``duplicate``, ``skipped`` (spam), ``rejected``
            (bad signature) or ``error`` — with a matching HTTP status code
            (see :data:`_INBOUND_STATUS_CODES`).
    """
    service: ConversationService = request.app.state.service
    provider_key = await _resolve_inbound_provider(request)
    request.app.state.log.info(
        "Inbound POST resolved to provider '%s'", provider_key
    )
    result = await service.handle_inbound(request, provider_key)
    status_code = _INBOUND_STATUS_CODES.get(result.get("status"), 200)
    return JSONResponse(content=result, status_code=status_code)


@router.get("/webhooks/rfq/inbound")
async def validate_inbound_webhook():
    """Answer the GET probe some providers send before saving a route.

    Elastic Email (and others) validate an inbound notification URL by
    issuing a ``GET`` and requiring a ``2xx`` response before they will save
    it. This handler exists solely to satisfy that probe. Deliberately
    public, same reasoning as the POST variant above.

    Returns:
        dict: ``{"status": "ok"}`` with an implicit ``200`` status.
    """
    return {"status": "ok"}
