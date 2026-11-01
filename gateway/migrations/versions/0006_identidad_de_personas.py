"""Identidad de personas (G10): contraseña y MFA de cada persona, sesiones y la
aplicación cuyas reglas se aplican a las claves personales.

Solo añade columnas y una tabla (compatible hacia atrás): las filas que ya existen quedan
sin contraseña, sin MFA y sin claves personales.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-01 06:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('user_sessions',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('refresh_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_used_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('max_expires_at', sa.DateTime(), nullable=False),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('revoked_reason', sa.String(length=32), nullable=True),
    sa.Column('ip_address', sa.String(length=45), nullable=True),
    sa.Column('user_agent', sa.String(length=255), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_sessions_user_id_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_sessions'))
    )
    op.create_index(op.f('ix_user_sessions_user_id'), 'user_sessions', ['user_id'], unique=False)
    op.add_column('applications', sa.Column('personal_keys', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column('users', sa.Column('password_hash', sa.String(length=255), nullable=True))
    op.add_column('users', sa.Column('password_changed_at', sa.DateTime(), nullable=True))
    op.add_column('users', sa.Column('mfa_secret_encrypted', sa.Text(), nullable=True))
    op.add_column('users', sa.Column('mfa_enabled', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column('users', sa.Column('mfa_last_step', sa.BigInteger(), nullable=True))
    op.add_column('users', sa.Column('last_login_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'last_login_at')
    op.drop_column('users', 'mfa_last_step')
    op.drop_column('users', 'mfa_enabled')
    op.drop_column('users', 'mfa_secret_encrypted')
    op.drop_column('users', 'password_changed_at')
    op.drop_column('users', 'password_hash')
    op.drop_column('applications', 'personal_keys')
    op.drop_table('user_sessions')
