"""Caché exacta (G11): cuánto guarda cada aplicación sus respuestas y para qué propósitos.

Solo añade columnas (compatible hacia atrás): las aplicaciones que ya existen quedan sin
caché (0 segundos).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-01 08:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('applications', sa.Column('cache_ttl_seconds', sa.Integer(), server_default='0', nullable=False))
    op.add_column('applications', sa.Column('cache_purposes', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('applications', 'cache_purposes')
    op.drop_column('applications', 'cache_ttl_seconds')
