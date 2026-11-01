"""Telemetría (G12): webhooks firmados, sus entregas y el tiempo propio del gateway en la auditoría.

Solo añade (compatible hacia atrás). `audit_logs.overhead_ms` queda fuera de la cadena de
G9: no cambia `hash_version` y las filas ya selladas siguen verificándose igual.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-01 09:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('webhooks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=True),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('url', sa.String(length=2048), nullable=False),
    sa.Column('events', sa.JSON(), nullable=False),
    sa.Column('secret_encrypted', sa.Text(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('consecutive_failures', sa.Integer(), server_default='0', nullable=False),
    sa.Column('disabled_reason', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_webhooks_organization_id_organizations'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_webhooks'))
    )
    op.create_index(op.f('ix_webhooks_organization_id'), 'webhooks', ['organization_id'], unique=False)
    op.create_table('webhook_deliveries',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('webhook_id', sa.Integer(), nullable=False),
    sa.Column('event_id', sa.String(length=32), nullable=False),
    sa.Column('event', sa.String(length=64), nullable=False),
    sa.Column('payload', sa.Text(), nullable=False),
    sa.Column('status', sa.Enum('pending', 'delivered', 'failed', 'cancelled', name='deliverystatus', native_enum=False, length=16), nullable=False),
    sa.Column('attempts', sa.SmallInteger(), server_default='0', nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(), nullable=False),
    sa.Column('last_status_code', sa.SmallInteger(), nullable=True),
    sa.Column('last_error', sa.String(length=64), nullable=True),
    sa.Column('last_duration_ms', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('delivered_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['webhook_id'], ['webhooks.id'], name=op.f('fk_webhook_deliveries_webhook_id_webhooks'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_webhook_deliveries'))
    )
    op.create_index(op.f('ix_webhook_deliveries_created_at'), 'webhook_deliveries', ['created_at'], unique=False)
    op.create_index('ix_webhook_deliveries_due', 'webhook_deliveries', ['status', 'next_attempt_at'], unique=False)
    op.create_index(op.f('ix_webhook_deliveries_webhook_id'), 'webhook_deliveries', ['webhook_id'], unique=False)
    op.add_column('audit_logs', sa.Column('overhead_ms', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('audit_logs', 'overhead_ms')
    # Los índices se van con sus tablas (el de la clave foránea no se puede borrar antes)
    op.drop_table('webhook_deliveries')
    op.drop_table('webhooks')
