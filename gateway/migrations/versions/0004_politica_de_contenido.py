"""Política de contenido (G8): acción por tipo de hallazgo, organización o aplicación y sentido.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-01 04:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('content_policies',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('scope', sa.Enum('organization', 'application', name='policyscope', native_enum=False,
                               length=16), nullable=False),
    sa.Column('scope_id', sa.Integer(), nullable=False),
    sa.Column('direction', sa.Enum('input', 'output', name='direction', native_enum=False, length=16),
              nullable=False),
    sa.Column('check_name', sa.Enum('jailbreak', 'classified', 'secret', 'pii', name='contentcheck',
                                    native_enum=False, length=16), nullable=False),
    sa.Column('action', sa.Enum('block', 'redact', 'log', 'off', name='contentaction', native_enum=False,
                                length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_content_policies')),
    sa.UniqueConstraint('scope', 'scope_id', 'direction', 'check_name', name='uq_content_policies_rule')
    )


def downgrade() -> None:
    op.drop_table('content_policies')
