"""Auditoría encadenada (G9): huella de la persona, estado de la cadena y disparadores que
impiden cambiar o borrar una fila sellada.

Una fila sin `chain_hash` (pendiente o aún sin sellar) se puede actualizar: así funciona la
escritura en dos pasos (ADR-007). Una vez sellada, solo se admite seudonimizar
`end_user_ref` (RGPD), y nunca se puede borrar. TRUNCATE no dispara los disparadores: queda
para los tests y es una orden de administrador, no de la aplicación.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-01 05:00:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

# Todas las columnas menos end_user_ref (lo único seudonimizable) y chain_hash
_FIXED = ["id", "request_id", "created_at", "completed_at", "organization_id", "team_id", "application_id",
          "user_id", "end_user_digest", "api_key_prefix", "purpose", "contract", "stream", "requested_model",
          "model_key", "deployment_id", "provider_slug", "status", "http_status", "error_type", "input_tokens",
          "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd", "latency_ms", "attempts",
          "prompt_hash", "hash_version", "security_flags"]
_SAME = "\n    AND ".join(f"OLD.{c} <=> NEW.{c}" for c in _FIXED)

NO_UPDATE = f"""
CREATE TRIGGER trg_audit_logs_sealed BEFORE UPDATE ON audit_logs FOR EACH ROW
BEGIN
  IF OLD.chain_hash IS NOT NULL AND NOT (
    {_SAME}
    AND OLD.chain_hash <=> NEW.chain_hash
    AND (NEW.end_user_ref <=> OLD.end_user_ref OR NEW.end_user_ref IS NULL OR NEW.end_user_ref LIKE 'seud:%')
  ) THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_logs: una fila sellada no se puede cambiar';
  END IF;
END
"""

NO_DELETE = """
CREATE TRIGGER trg_audit_logs_no_delete BEFORE DELETE ON audit_logs FOR EACH ROW
BEGIN
  SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'audit_logs: las filas no se borran';
END
"""


def _check_trigger_privilege() -> None:
    """Con el log binario activo, MySQL solo deja crear disparadores a quien tiene SUPER o si
    `log_bin_trust_function_creators` está activado. Se comprueba antes de cambiar nada: en
    MySQL cada cambio de esquema se confirma solo y una migración a medias no se repite bien."""
    conn = op.get_bind()
    log_bin, trusted = conn.execute(sa.text("SELECT @@log_bin, @@log_bin_trust_function_creators")).one()
    if not int(log_bin) or int(trusted):
        return
    grants = " ".join(row[0] for row in conn.execute(sa.text("SHOW GRANTS")))
    if "SUPER" in grants or "ALL PRIVILEGES ON *.*" in grants:
        return
    raise RuntimeError(
        "MySQL no deja crear los disparadores de la auditoría: el log binario está activo y el usuario "
        "no tiene SUPER. Arranque MySQL con --log-bin-trust-function-creators=1 (ya está en "
        "docker-compose.yml; en un MySQL gestionado, en su grupo de parámetros) y repita la migración.")


def upgrade() -> None:
    _check_trigger_privilege()
    op.add_column('audit_logs', sa.Column('end_user_digest', sa.String(length=64), nullable=True))
    op.create_table('audit_chain_state',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('last_id', sa.BigInteger(), nullable=False),
    sa.Column('last_hash', sa.String(length=64), nullable=False),
    sa.Column('updated_at', mysql.DATETIME(fsp=6), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_chain_state'))
    )
    op.execute(NO_UPDATE)
    op.execute(NO_DELETE)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_logs_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_audit_logs_sealed")
    op.drop_table('audit_chain_state')
    op.drop_column('audit_logs', 'end_user_digest')
