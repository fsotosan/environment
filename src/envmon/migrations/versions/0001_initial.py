"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "samples",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.BigInteger(), nullable=False),
        sa.Column("temperature_c", sa.Float(), nullable=True),
        sa.Column("humidity_pct", sa.Float(), nullable=True),
        sa.Column("pm1_0_ugm3", sa.Float(), nullable=True),
        sa.Column("pm2_5_ugm3", sa.Float(), nullable=True),
        sa.Column("pm4_0_ugm3", sa.Float(), nullable=True),
        sa.Column("pm10_ugm3", sa.Float(), nullable=True),
        sa.Column("voc_index", sa.Float(), nullable=True),
        sa.Column("nox_index", sa.Float(), nullable=True),
        sa.Column("lux", sa.Float(), nullable=True),
        sa.Column("noise_db_a", sa.Float(), nullable=True),
        sa.Column("noise_peak_db", sa.Float(), nullable=True),
        sa.Column("noise_dbfs", sa.Float(), nullable=True),
        sa.Column("noise_sample_rate_hz", sa.Integer(), nullable=True),
        sa.Column("noise_clipping_pct", sa.Float(), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("errors", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_samples_ts", "samples", ["ts"], unique=True)

    op.create_table(
        "daily_stats",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("temperature_avg", sa.Float(), nullable=True),
        sa.Column("temperature_min", sa.Float(), nullable=True),
        sa.Column("temperature_max", sa.Float(), nullable=True),
        sa.Column("humidity_avg", sa.Float(), nullable=True),
        sa.Column("humidity_min", sa.Float(), nullable=True),
        sa.Column("humidity_max", sa.Float(), nullable=True),
        sa.Column("pm2_5_avg", sa.Float(), nullable=True),
        sa.Column("pm2_5_max", sa.Float(), nullable=True),
        sa.Column("pm10_avg", sa.Float(), nullable=True),
        sa.Column("pm10_max", sa.Float(), nullable=True),
        sa.Column("voc_avg", sa.Float(), nullable=True),
        sa.Column("voc_max", sa.Float(), nullable=True),
        sa.Column("nox_avg", sa.Float(), nullable=True),
        sa.Column("nox_max", sa.Float(), nullable=True),
        sa.Column("lux_avg", sa.Float(), nullable=True),
        sa.Column("lux_min", sa.Float(), nullable=True),
        sa.Column("lux_max", sa.Float(), nullable=True),
        sa.Column("noise_avg", sa.Float(), nullable=True),
        sa.Column("noise_max", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("day"),
    )

    op.create_table(
        "llm_summaries",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("day"),
    )


def downgrade() -> None:
    op.drop_table("llm_summaries")
    op.drop_table("daily_stats")
    op.drop_index("ix_samples_ts", table_name="samples")
    op.drop_table("samples")
