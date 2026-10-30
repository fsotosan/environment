"""noise interval statistics

Replace the single per-sample ``noise_db_a`` value with interval Leq, maximum
and L10/L50/L90. Existing values are copied into every new column so historical
rows keep a usable, if not truly percentile-based, reading.

Revision ID: 0002_noise_interval
Revises: 0001_initial
Create Date: 2026-10-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_noise_interval"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_SAMPLE_COLUMNS = (
    "noise_leq_db_a",
    "noise_max_db_a",
    "noise_l10_db_a",
    "noise_l50_db_a",
    "noise_l90_db_a",
)


def upgrade() -> None:
    with op.batch_alter_table("samples") as batch_op:
        for name in _NEW_SAMPLE_COLUMNS:
            batch_op.add_column(sa.Column(name, sa.Float(), nullable=True))

    # The old value was a short-term estimate, not an interval Leq. Copy it into
    # every new field rather than inventing a distribution.
    assignments = ", ".join(f"{name} = noise_db_a" for name in _NEW_SAMPLE_COLUMNS)
    op.execute(f"UPDATE samples SET {assignments} WHERE noise_db_a IS NOT NULL")

    with op.batch_alter_table("samples") as batch_op:
        batch_op.drop_column("noise_db_a")

    with op.batch_alter_table("daily_stats") as batch_op:
        batch_op.alter_column("noise_avg", new_column_name="noise_leq_avg")


def downgrade() -> None:
    with op.batch_alter_table("daily_stats") as batch_op:
        batch_op.alter_column("noise_leq_avg", new_column_name="noise_avg")

    with op.batch_alter_table("samples") as batch_op:
        batch_op.add_column(sa.Column("noise_db_a", sa.Float(), nullable=True))

    op.execute("UPDATE samples SET noise_db_a = noise_leq_db_a WHERE noise_leq_db_a IS NOT NULL")

    with op.batch_alter_table("samples") as batch_op:
        for name in _NEW_SAMPLE_COLUMNS:
            batch_op.drop_column(name)
