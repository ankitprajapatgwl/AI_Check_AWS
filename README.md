# AI Check AWS

Two proofs of concept, served by **one process on one port** from this one
repo (`src/app.py` mounts the Bedrock POC's routes into the same FastAPI app
EmailPOC runs):

- **EmailPOC** — RFQ (Request for Quotation) email conversations with
  suppliers, threaded by RFC `Message-ID` plus an `[RFQ - id]` subject
  prefix, backed by PostgreSQL. Served at **`/email_poc`**. See
  [Conversation lifecycle & threading](#conversation-lifecycle--threading)
  below, plus [`RFQ_EMAIL_FLOW.md`](RFQ_EMAIL_FLOW.md),
  [`registration_guide.md`](registration_guide.md) and
  [`email_tracking.md`](email_tracking.md) for how the app itself works.
- **Bedrock Availability POC** ([`bedrock_availability_poc/`](bedrock_availability_poc/)) —
  checks which AI model providers (Claude, DeepSeek, Qwen, ChatGPT, Zhipu
  GLM) your AWS account can actually access on Amazon Bedrock, by really
  invoking each one. Served at **`/check-bedrock`**. See
  [`bedrock_availability_poc/README.md`](bedrock_availability_poc/README.md)
  for how the check itself works (that doc also still covers running it
  entirely standalone, on its own port, if you want that instead).

Once running, open **`/`** — a landing page with three sections that link
into both: running the Bedrock check, a "default user" login bypass, and
the normal EmailPOC register/login/track flow.

This README covers **installation only** — running the stack with Docker,
or running it manually.

---

## Option A: Docker (recommended — no Python setup required)

Runs two containers with one command: Postgres, and the app (EmailPOC +
Bedrock Availability POC together, one image, one port).

### 1. Install Docker Desktop

Download and install [Docker Desktop](https://www.docker.com/products/docker-desktop/)
and make sure it's running. That's the only prerequisite — you do **not**
need Python, `uv`, or Postgres installed on your machine.

### 2. Get credentials

- **EmailPOC**: an `API_USER`/`API_KEY` pair from either
  [EngageLab](setup_docs/engagelab_guide/engagelab_setup.md) or
  [SendCloud](setup_docs/aurora_send_cloud), plus a sending domain — or, for
  [Alibaba Enterprise Mail](setup_docs/alibaba_guide/Alibaba_Documentation.md),
  a mailbox and its third-party client security password (Alibaba is SMTP/IMAP
  rather than a REST API).
- **Bedrock Availability POC**: AWS credentials (access key/secret, or a
  profile) with `bedrock:ListFoundationModels` and `bedrock:InvokeModel`.
  Optionally an `ANTHROPIC_API_KEY` for the extra direct-API check.

You can skip this step to just click through the UI without sending mail or
running a real Bedrock check — the app still starts, it just can't do the
real work until these are set.

### 3. Run it

```bash
make up
```

**No `make`?**

```bash
cp .env.docker.example .env
docker compose up -d --build
```

Or install `make`:

| Platform            | How                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **macOS**           | `xcode-select --install` (Xcode Command Line Tools includes `make`), or `brew install make` if you use Homebrew                                                                                                                                                                                                                                                                                                           |
| **Ubuntu / Debian** | `sudo apt update && sudo apt install make`                                                                                                                                                                                                                                                                                                                                                                                |
| **Windows**         | `make` isn't native. Easiest is to just double-click [`run.bat`](run.bat) instead — it runs the same `docker compose` commands with no `make` needed. If you do want `make`: install via [Chocolatey](Chocolatey - The package manager for Windows) (`choco install make`), [Scoop](https://scoop.sh/) (`scoop install make`), or use WSL (Windows Subsystem for Linux) and install it there with `sudo apt install make` |

The first run creates `.env` from [`.env.docker.example`](.env.docker.example)
and stops so you can fill in your credentials. Open `.env`, fill in
`SECRET_KEY`, the EngageLab and/or SendCloud block (`{PROVIDER}_API_USER`,
`{PROVIDER}_API_KEY`, `{PROVIDER}_OUTBOUND_DOMAIN`), and the AWS/Anthropic
variables — then run the same command again.
This time it builds the image, starts Postgres, waits for it to be
healthy, and starts the app container. Its entrypoint also applies
database migrations automatically on every start (safe to re-run — see
[`docker/entrypoint.sh`](docker/entrypoint.sh)).

Once it's up, open:

| URL                                    | What it is                        |
| -------------------------------------- | --------------------------------- |
| `http://localhost:8000/`               | Landing page — links to both POCs |
| `http://localhost:8000/email_poc/`     | EmailPOC                          |
| `http://localhost:8000/check-bedrock/` | Bedrock Availability check (JSON) |

### Everyday commands

| Command            | What it does                                                                               |
| ------------------ | ------------------------------------------------------------------------------------------ |
| `make up`          | Build (if needed) and start everything                                                     |
| `make logs`        | Follow every container's logs                                                              |
| `make down`        | Stop everything (data is kept)                                                             |
| `make restart`     | Restart the containers                                                                     |
| `make ps`          | Show container status                                                                      |
| `make migrate`     | Manually re-run EmailPOC's database migrations                                             |
| `make reset-db`    | Stop everything **and delete the database volume**                                         |
| `make john-carter` | Open EmailPOC pre-logged-in as the demo "John Carter" user (needs `DEV_BYPASS_LOGIN=true`) |

No `make`? Run the equivalent `docker compose ...` commands directly — see
the [`Makefile`](Makefile), each target is a one-liner.

---

## Option B: Manual (no Docker)

One process serves both POCs — start it once and both `/email_poc` and
`/check-bedrock` are live.

Prerequisites: Python ≥ 3.11, [uv](https://docs.astral.sh/uv/), PostgreSQL
16 (or just `docker compose up -d db`), and an AWS account with Bedrock
access for the Bedrock check.

```bash
# 1. Install dependencies (uv creates .venv automatically; this also pulls
#    in boto3/rich/anthropic for the Bedrock POC — see pyproject.toml)
uv sync

# 2. Start Postgres (or point DATABASE_URL at one you already have)
docker compose up -d db

# 3. Configure environment
cp .env.example .env
nano .env            # EMAIL_PROVIDER + its credentials, and AWS_REGION + credentials

# 4. Apply database migrations
uv run alembic upgrade head

# 5. Run the hot-reload development server
uv run python main.py
# or directly:
uv run uvicorn src.app:app --host 0.0.0.0 --port 8000 --reload
```

Open **http://localhost:8000/** (landing page), or go straight to
**http://localhost:8000/email_poc/** or
**http://localhost:8000/check-bedrock/**. See
[`bedrock_availability_poc/README.md`](bedrock_availability_poc/README.md)
for the full breakdown of what the Bedrock check does and how to read its
output (and how to run that POC entirely on its own, on its own port,
instead of merged into this app).

---

## Environment variables

- [`.env.docker.example`](.env.docker.example) — the template for the
  Docker quick start above (Option A). One `.env`, shared by both
  containers.
- [`.env.example`](.env.example) — the template for manual/local EmailPOC
  development (Option B), covering EngageLab, SendCloud, Alibaba Enterprise
  Mail, `MESSAGE_ID_DOMAIN`, and the Bedrock Availability POC's
  AWS/Anthropic variables.
- [`bedrock_availability_poc/.env.example`](bedrock_availability_poc/.env.example) —
  the template for running the Bedrock POC entirely standalone (its own
  `.env`, independent of the root one).

Copy whichever applies to `.env` and fill in your values — each app fails
fast at startup with a clear message if something required is missing.

---

## Conversation lifecycle & threading

### Lifecycle

A conversation is created **before** anything is sent, so a failed send
still leaves a record of what was attempted:

```
  create draft            send RFQ              reply matched
 ─────────────►  draft  ───────────►  open  ───────────────►  closed
                          │
                          └── provider rejected ──►  failed
```

| Status   | Meaning                                                       |
| -------- | ------------------------------------------------------------- |
| `draft`  | `conv_id` and subject assigned; nothing sent yet              |
| `open`   | RFQ sent and accepted by the provider; awaiting a reply       |
| `closed` | A supplier reply was matched to it                            |
| `failed` | The provider rejected the send                                |

The old keyword classifier still runs, but its verdict (`QUOTE_RECEIVED`,
`DECLINED`, …) is stored in `conversations.last_action` for information
only — it no longer decides the status.

### Subject contract

Every outbound RFQ subject is exactly:

```
[RFQ - {conv_id}] - {subject line}
```

e.g. `[RFQ - hd273hsd] - Request for Quotation — Stainless Steel Water Bottle`
(Chinese suppliers get `询价请求 — …`). **Keeping this prefix intact is all a
supplier has to do for their reply to be matched.**

### How a reply is matched

There is no per-conversation reply address. Each outbound RFQ carries an
app-minted `Message-ID` (`<rfq.{conv_id}.{uuid}@{MESSAGE_ID_DOMAIN}>`) and an
`X-RFQ-Conversation-Id` header, and inbound mail is matched in this order:

| # | Signal                                    | `matched_via`    |
| - | ----------------------------------------- | ---------------- |
| 1 | `In-Reply-To` / `References` → our stored `Message-ID` | `message_id`     |
| 2 | `X-RFQ-Conversation-Id` header            | `header_token`   |
| 3 | `[RFQ - id]` subject prefix               | `subject_token`  |
| 4 | *(none matched)* → stored in `unmatched_emails` + `unmatched_attachments`, nothing dropped | — |

Step 1 is the correct answer whenever the supplier used their client's Reply
button; step 3 is the guaranteed fallback that survives header-stripping
gateways and manual forwards. `matched_via` is shown on every received
message in the conversation thread, which makes threading regressions
obvious at a glance.

Re-delivering the same message (a webhook retry, an IMAP re-poll) is a no-op
— `emails.message_id` is `UNIQUE` and is checked before insert.

### Inbound URL

There is one inbound URL. Point every provider's inbound webhook at it:

```
/email_poc/webhooks/rfq/inbound
```

No provider names itself — EngageLab's WebHook field and SendCloud/Aurora's
Inbound Route just re-post to the URL string you typed, and neither can
append a path segment — so the app works out who posted from the payload's
own shape (see `_resolve_inbound_provider` in [`src/route.py`](src/route.py)).

One case that shape can't settle: SendCloud's payload is undocumented and
this repo's parser mirrors SendGrid's field names exactly. Configure
SendCloud with the optional override
`/email_poc/webhooks/rfq/inbound?provider=sendcloud`; every other provider
uses the bare URL.

**Alibaba is the exception**: it has no inbound webhook, so its replies are
polled over IMAP by a background task instead — see
[`setup_docs/alibaba_guide/Alibaba_Documentation.md`](setup_docs/alibaba_guide/Alibaba_Documentation.md).

### Providers and regions

The Send RFQ form takes a **Provider** and a **Supplier Type**, and the pair
resolves to the region actually used:

| Provider           | Chinese →      | Non-Chinese → |
| ------------------ | -------------- | ------------- |
| SendCloud          | `sendcloud_hk` | `sendcloud`   |
| EngageLab          | `engagelab`    | `engagelab`   |
| Alibaba Enterprise | `alibaba_hk`   | `alibaba`     |

Chinese suppliers also get the Simplified-Chinese body template
(`templates/emails/rfq_email_zh.html`); everyone else gets the English one.
Both are **HTML only** — no `text/plain` alternative is produced anywhere.
