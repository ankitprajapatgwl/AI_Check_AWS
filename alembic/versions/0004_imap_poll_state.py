"""IMAP poll cursor — stop using \\Seen as the "already handled" marker

The Alibaba IMAP poller searched ``UNSEEN`` and flagged each message ``\\Seen``
once processed. That makes the read flag load-bearing, so anything a human
opened first (webmail preview pane, phone client) was never fetched at all —
silently dropped, with no row in ``emails`` or ``unmatched_emails``.

``imap_poll_state`` replaces the flag with a durable per-mailbox high-water
mark. IMAP UIDs only increase within a ``UIDVALIDITY`` generation, so
``UID {last_uid + 1}:*`` is an exact, read-state-independent definition of
"new mail".

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-06

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "imap_poll_state",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # (address, folder) rather than region: the Singapore and Hong Kong
        # access points front the same mailbox, so both share one cursor.
        sa.Column("account", sa.String(), nullable=False),
        sa.Column("mailbox", sa.String(), nullable=False),
        sa.Column("uid_validity", sa.String(), nullable=False),
        # UIDs are 32-bit unsigned per RFC 3501; BIGINT holds the whole range.
        sa.Column(
            "last_uid", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "account", "mailbox", name="uq_imap_poll_state_account_mailbox"
        ),
    )


def downgrade() -> None:
    op.drop_table("imap_poll_state")
