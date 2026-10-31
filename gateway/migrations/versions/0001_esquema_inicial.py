"""Esquema inicial (G2): identidad, claves, catálogo de LLM, precios y auditoría.

Revision ID: 0001
Revises: 
Create Date: 2026-10-31 09:01:02.090574
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('audit_logs',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('request_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', mysql.DATETIME(fsp=6), nullable=False),
    sa.Column('completed_at', mysql.DATETIME(fsp=6), nullable=True),
    sa.Column('organization_id', sa.Integer(), nullable=True),
    sa.Column('team_id', sa.Integer(), nullable=True),
    sa.Column('application_id', sa.Integer(), nullable=True),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('end_user_ref', sa.String(length=128), nullable=True),
    sa.Column('api_key_prefix', sa.String(length=48), nullable=True),
    sa.Column('purpose', sa.String(length=64), nullable=True),
    sa.Column('contract', sa.Enum('anthropic', 'openai', name='contract', native_enum=False, length=16), nullable=False),
    sa.Column('stream', sa.Boolean(), nullable=False),
    sa.Column('requested_model', sa.String(length=128), nullable=True),
    sa.Column('model_key', sa.String(length=128), nullable=True),
    sa.Column('deployment_id', sa.Integer(), nullable=True),
    sa.Column('provider_slug', sa.String(length=64), nullable=True),
    sa.Column('status', sa.Enum('pending', 'success', 'error', 'blocked', name='auditstatus', native_enum=False, length=16), nullable=False),
    sa.Column('http_status', sa.SmallInteger(), nullable=True),
    sa.Column('error_type', sa.String(length=64), nullable=True),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('cache_read_tokens', sa.Integer(), nullable=False),
    sa.Column('cache_write_tokens', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Numeric(precision=14, scale=8), nullable=False),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('prompt_hash', sa.String(length=64), nullable=True),
    sa.Column('hash_version', sa.String(length=4), nullable=False),
    sa.Column('security_flags', sa.JSON(), nullable=True),
    sa.Column('chain_hash', sa.String(length=64), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_logs'))
    )
    op.create_index('ix_audit_logs_app_created', 'audit_logs', ['application_id', 'created_at'], unique=False)
    op.create_index('ix_audit_logs_org_created', 'audit_logs', ['organization_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_audit_logs_request_id'), 'audit_logs', ['request_id'], unique=False)
    op.create_table('llm_models',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('model_key', sa.String(length=128), nullable=False),
    sa.Column('display_name', sa.String(length=255), nullable=False),
    sa.Column('family', sa.Enum('anthropic', 'openai', name='modelfamily', native_enum=False, length=32), nullable=False),
    sa.Column('context_window', sa.Integer(), nullable=True),
    sa.Column('max_output_tokens', sa.Integer(), nullable=True),
    sa.Column('capabilities', sa.JSON(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_llm_models')),
    sa.UniqueConstraint('model_key', name=op.f('uq_llm_models_model_key'))
    )
    op.create_table('organizations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('allowed_providers', sa.JSON(), nullable=True),
    sa.Column('audit_salt', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_organizations')),
    sa.UniqueConstraint('slug', name=op.f('uq_organizations_slug'))
    )
    op.create_table('providers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('kind', sa.Enum('anthropic', 'openai_compatible', name='providerkind', native_enum=False, length=32), nullable=False),
    sa.Column('base_url', sa.String(length=512), nullable=True),
    sa.Column('api_key_encrypted', sa.Text(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_providers')),
    sa.UniqueConstraint('slug', name=op.f('uq_providers_slug'))
    )
    op.create_table('deployments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('model_id', sa.Integer(), nullable=False),
    sa.Column('provider_id', sa.Integer(), nullable=False),
    sa.Column('upstream_model', sa.String(length=255), nullable=False),
    sa.Column('weight', sa.Integer(), nullable=False),
    sa.Column('priority', sa.Integer(), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['model_id'], ['llm_models.id'], name=op.f('fk_deployments_model_id_llm_models'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['provider_id'], ['providers.id'], name=op.f('fk_deployments_provider_id_providers'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_deployments')),
    sa.UniqueConstraint('model_id', 'provider_id', 'upstream_model', name='uq_deployments_model_provider_upstream')
    )
    op.create_index(op.f('ix_deployments_model_id'), 'deployments', ['model_id'], unique=False)
    op.create_index(op.f('ix_deployments_provider_id'), 'deployments', ['provider_id'], unique=False)
    op.create_table('model_prices',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('model_id', sa.Integer(), nullable=False),
    sa.Column('effective_from', sa.DateTime(), nullable=False),
    sa.Column('input_per_mtok', sa.Numeric(precision=12, scale=6), nullable=False),
    sa.Column('output_per_mtok', sa.Numeric(precision=12, scale=6), nullable=False),
    sa.Column('cache_read_per_mtok', sa.Numeric(precision=12, scale=6), nullable=True),
    sa.Column('cache_write_per_mtok', sa.Numeric(precision=12, scale=6), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['model_id'], ['llm_models.id'], name=op.f('fk_model_prices_model_id_llm_models'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_model_prices')),
    sa.UniqueConstraint('model_id', 'effective_from', name='uq_model_prices_model_from')
    )
    op.create_index(op.f('ix_model_prices_model_id'), 'model_prices', ['model_id'], unique=False)
    op.create_table('teams',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('parent_id', sa.Integer(), nullable=True),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_teams_organization_id_organizations'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['parent_id'], ['teams.id'], name=op.f('fk_teams_parent_id_teams'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_teams')),
    sa.UniqueConstraint('organization_id', 'slug', name='uq_teams_org_slug')
    )
    op.create_index(op.f('ix_teams_organization_id'), 'teams', ['organization_id'], unique=False)
    op.create_table('applications',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('team_id', sa.Integer(), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('status', sa.Enum('active', 'suspended', 'revoked', name='applicationstatus', native_enum=False, length=16), nullable=False),
    sa.Column('trusted', sa.Boolean(), nullable=False),
    sa.Column('allowed_purposes', sa.JSON(), nullable=False),
    sa.Column('allowed_models', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_applications_organization_id_organizations'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_applications_team_id_teams'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_applications')),
    sa.UniqueConstraint('organization_id', 'slug', name='uq_applications_org_slug')
    )
    op.create_index(op.f('ix_applications_organization_id'), 'applications', ['organization_id'], unique=False)
    op.create_index(op.f('ix_applications_team_id'), 'applications', ['team_id'], unique=False)
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('team_id', sa.Integer(), nullable=True),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('role', sa.Enum('admin', 'member', name='userrole', native_enum=False, length=16), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_users_organization_id_organizations'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['team_id'], ['teams.id'], name=op.f('fk_users_team_id_teams'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('organization_id', 'email', name='uq_users_org_email')
    )
    op.create_index(op.f('ix_users_organization_id'), 'users', ['organization_id'], unique=False)
    op.create_index(op.f('ix_users_team_id'), 'users', ['team_id'], unique=False)
    op.create_table('api_keys',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.Enum('application', 'user', name='keykind', native_enum=False, length=16), nullable=False),
    sa.Column('application_id', sa.Integer(), nullable=True),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('prefix', sa.String(length=48), nullable=False),
    sa.Column('key_hash', sa.String(length=64), nullable=False),
    sa.Column('label', sa.String(length=255), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=True),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('last_used_at', sa.DateTime(), nullable=True),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.CheckConstraint("(kind = 'application' AND application_id IS NOT NULL AND user_id IS NULL) OR (kind = 'user' AND user_id IS NOT NULL AND application_id IS NULL)", name=op.f('ck_api_keys_owner_matches_kind')),
    sa.ForeignKeyConstraint(['application_id'], ['applications.id'], name=op.f('fk_api_keys_application_id_applications'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_api_keys_organization_id_organizations'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_api_keys_user_id_users'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_api_keys')),
    sa.UniqueConstraint('key_hash', name=op.f('uq_api_keys_key_hash'))
    )
    op.create_index(op.f('ix_api_keys_application_id'), 'api_keys', ['application_id'], unique=False)
    op.create_index(op.f('ix_api_keys_organization_id'), 'api_keys', ['organization_id'], unique=False)
    op.create_index(op.f('ix_api_keys_user_id'), 'api_keys', ['user_id'], unique=False)


def downgrade() -> None:
    # Se borran las tablas en orden inverso; sus índices y claves foráneas se van con ellas
    # (borrar antes un índice que respalda una clave foránea falla en MySQL)
    for table in ("api_keys", "users", "applications", "teams", "model_prices", "deployments", "providers",
                  "organizations", "llm_models", "audit_logs"):
        op.drop_table(table)
