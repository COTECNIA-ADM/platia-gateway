"""Límites de uso (G7): cuotas de gasto, peticiones y tokens, y peticiones simultáneas.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-31 18:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('usage_limits',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('scope', sa.Enum('organization', 'team', 'application', 'end_user', name='limitscope',
                               native_enum=False, length=16), nullable=False),
    sa.Column('scope_id', sa.Integer(), nullable=False),
    sa.Column('subject', sa.String(length=128), nullable=True),
    sa.Column('metric', sa.Enum('cost_usd', 'requests', 'tokens', 'concurrent', name='limitmetric',
                                native_enum=False, length=16), nullable=False),
    sa.Column('period', sa.Enum('minute', 'day', 'month', 'none', name='limitperiod',
                                native_enum=False, length=16), nullable=False),
    sa.Column('limit_value', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('alert_pct', sa.SmallInteger(), server_default='80', nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_usage_limits'))
    )
    op.create_index('ix_usage_limits_scope', 'usage_limits', ['scope', 'scope_id'], unique=False)
    op.create_index('ix_audit_logs_team_created', 'audit_logs', ['team_id', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_audit_logs_team_created', table_name='audit_logs')
    op.drop_index('ix_usage_limits_scope', table_name='usage_limits')
    op.drop_table('usage_limits')
