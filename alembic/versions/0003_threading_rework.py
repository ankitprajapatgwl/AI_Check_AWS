"""RFQ threading rework — draft/open/closed lifecycle + real Message-IDs

Implements RFQ_THREADING_IMPLEMENTATION_PLAN.md §3 (Phase 1):

* ``conversations`` gains the ``draft``/``open``/``closed``/``failed``
  lifecycle, ``reply_to_address`` is renamed to ``from_address`` (the random
  per-conversation dynamic address scheme is gone), and the columns the new
  draft-first flow needs (``supplier_type``, ``send_key``, ``last_action``,
  ``sent_at``, ``closed_at``) are added.
* ``emails`` gains ``provider_message_id`` and ``status_code`` — the
  provider's own id was previously built at send time and then thrown away.
* ``unmatched_emails`` gains the full inbound payload (subject, both bodies,
  the threading headers, the provider) plus a new ``unmatched_attachments``
  child table, so a cold email that matches nothing is still fully retained
  for future reference (requirement 4, step 4).

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-06

"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── conversations ────────────────────────────────────────────────────
    # The old verdict-driven statuses collapse into 'closed': a conversation
    # is closed the moment any supplier reply binds to it, and the keyword
    # classifier's verdict moves to the informational last_action column.
    op.drop_constraint(
        "ck_conversations_status", "conversations", type_="check"
    )
    op.execute(
        "UPDATE conversations SET status = 'closed' "
        "WHERE status IN ('replied', 'declined')"
    )
    op.create_check_constraint(
        "ck_conversations_status",
        "conversations",
        "status IN ('draft', 'open', 'closed', 'failed')",
    )
    op.alter_column(
        "conversations", "status", server_default="draft"
    )

    op.alter_column(
        "conversations", "reply_to_address", new_column_name="from_address"
    )
    # A draft exists before anything is sent, so there is no From address and
    # (when created from Quick Send with no provider yet) no provider either.
    op.alter_column("conversations", "from_address", nullable=True)
    op.alter_column("conversations", "provider", nullable=True)

    op.add_column(
        "conversations", sa.Column("supplier_type", sa.String(), nullable=True)
    )
    op.add_column(
        "conversations", sa.Column("send_key", sa.String(), nullable=True)
    )
    op.add_column(
        "conversations", sa.Column("last_action", sa.String(), nullable=True)
    )
    op.add_column(
        "conversations",
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    # ── emails ───────────────────────────────────────────────────────────
    # message_id already carries uq_emails_message_id (which indexes it) —
    # that UNIQUE constraint is what makes inbound ingestion idempotent now
    # that the column holds the real RFC header value instead of a fabricated
    # one, so it stays exactly as it is.
    op.add_column(
        "emails", sa.Column("provider_message_id", sa.String(), nullable=True)
    )
    op.add_column(
        "emails", sa.Column("status_code", sa.Integer(), nullable=True)
    )
    op.create_index(
        "ix_emails_provider_message_id", "emails", ["provider_message_id"]
    )

    # ── unmatched_emails ─────────────────────────────────────────────────
    op.add_column(
        "unmatched_emails", sa.Column("subject", sa.String(), nullable=True)
    )
    op.add_column(
        "unmatched_emails", sa.Column("body_text", sa.Text(), nullable=True)
    )
    op.add_column(
        "unmatched_emails", sa.Column("body_html", sa.Text(), nullable=True)
    )
    op.add_column(
        "unmatched_emails", sa.Column("provider", sa.String(), nullable=True)
    )
    op.add_column(
        "unmatched_emails", sa.Column("message_id", sa.String(), nullable=True)
    )
    op.add_column(
        "unmatched_emails", sa.Column("in_reply_to", sa.String(), nullable=True)
    )
    op.add_column(
        "unmatched_emails",
        sa.Column("references_header", sa.Text(), nullable=True),
    )
    op.add_column(
        "unmatched_emails",
        sa.Column("received_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_unmatched_emails_message_id", "unmatched_emails", ["message_id"]
    )

    op.create_table(
        "unmatched_attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "unmatched_email_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("unmatched_emails.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("content_type", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_unmatched_attachments_email_id",
        "unmatched_attachments",
        ["unmatched_email_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_unmatched_attachments_email_id", table_name="unmatched_attachments"
    )
    op.drop_table("unmatched_attachments")

    op.drop_index("ix_unmatched_emails_message_id", table_name="unmatched_emails")
    for column in (
        "received_at",
        "references_header",
        "in_reply_to",
        "message_id",
        "provider",
        "body_html",
        "body_text",
        "subject",
    ):
        op.drop_column("unmatched_emails", column)

    op.drop_index("ix_emails_provider_message_id", table_name="emails")
    op.drop_column("emails", "status_code")
    op.drop_column("emails", "provider_message_id")

    for column in (
        "closed_at",
        "sent_at",
        "last_action",
        "send_key",
        "supplier_type",
    ):
        op.drop_column("conversations", column)

    # Anything still in a status the old constraint forbids has to be mapped
    # back before the constraint can be restored: 'draft'/'failed' have no
    # pre-rework equivalent, so they become 'open' (never sent successfully),
    # and 'closed' returns to 'replied'.
    op.execute(
        "UPDATE conversations SET status = 'open' "
        "WHERE status IN ('draft', 'failed')"
    )
    op.execute(
        "UPDATE conversations SET status = 'replied' WHERE status = 'closed'"
    )
    op.execute(
        "UPDATE conversations SET from_address = '' WHERE from_address IS NULL"
    )
    op.execute("UPDATE conversations SET provider = '' WHERE provider IS NULL")
    op.alter_column("conversations", "provider", nullable=False)
    op.alter_column("conversations", "from_address", nullable=False)
    op.alter_column(
        "conversations", "from_address", new_column_name="reply_to_address"
    )

    op.drop_constraint(
        "ck_conversations_status", "conversations", type_="check"
    )
    op.alter_column("conversations", "status", server_default="open")
    op.create_check_constraint(
        "ck_conversations_status",
        "conversations",
        "status IN ('open', 'replied', 'declined')",
    )
