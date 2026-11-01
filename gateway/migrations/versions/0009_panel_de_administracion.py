"""Panel de administración (G13): registro de cambios y quién opera el gateway.

Solo añade (compatible hacia atrás): nadie es operador hasta que se le dé con la CLI.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-01 10:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('admin_actions',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=True),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('target_type', sa.String(length=32), nullable=False),
    sa.Column('target_id', sa.String(length=64), nullable=True),
    sa.Column('changes', sa.JSON(), nullable=True),
    sa.Column('ip_address', sa.String(length=45), nullable=True),
    sa.Column('request_id', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_admin_actions'))
    )
    op.create_index('ix_admin_actions_org_created', 'admin_actions', ['organization_id', 'created_at'], unique=False)
    op.add_column('users', sa.Column('platform_operator', sa.Boolean(), server_default='0', nullable=False))


def downgrade() -> None:
    op.drop_column('users', 'platform_operator')
    op.drop_table('admin_actions')
