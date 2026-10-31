"""Reserva entre despliegues y modelos (G6): cadena de reserva por modelo e intentos en la auditoría.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-31 12:10:00
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('llm_models', sa.Column('fallback_models', sa.JSON(), nullable=True))
    op.add_column('audit_logs', sa.Column('attempts', sa.SmallInteger(), server_default='1', nullable=False))


def downgrade() -> None:
    op.drop_column('audit_logs', 'attempts')
    op.drop_column('llm_models', 'fallback_models')
