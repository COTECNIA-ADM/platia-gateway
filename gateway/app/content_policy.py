"""Política de contenido de una petición y registro de lo encontrado (G8, ADR-012).

Lo usan las etapas `security_input`, `filter` y `security_output` y el streaming: aquí se
resuelve qué acción toca a cada tipo de hallazgo (aplicación > organización > por defecto)
y se anota lo encontrado en el contexto, que la auditoría guarda en `security_flags`.
"""
from collections import Counter

import structlog
from sqlalchemy import and_, or_, select

from .content import TextSlot, find, redact, tally
from .errors import ContentBlocked
from .models import ContentAction, ContentCheck, ContentPolicy, Direction, PolicyScope
from .pipeline.context import Deps, PipelineContext

log = structlog.get_logger(__name__)

A, C = ContentAction, ContentCheck
DEFAULTS: dict[tuple[Direction, ContentCheck], ContentAction] = {
    (Direction.INPUT, C.JAILBREAK): A.BLOCK,
    (Direction.INPUT, C.CLASSIFIED): A.LOG,
    (Direction.INPUT, C.SECRET): A.REDACT,
    (Direction.INPUT, C.PII): A.LOG,
    # En la salida, por defecto solo se anota: los secretos de la entrada ya van tachados y así
    # el streaming sigue reenviando los eventos tal cual (ADR-008). Tachar es opcional.
    (Direction.OUTPUT, C.SECRET): A.LOG,
    (Direction.OUTPUT, C.PII): A.LOG,
}
# Lo que tiene sentido en cada casilla: una marca de clasificación o un intento de
# manipulación no se arreglan tachando, y en la salida solo se buscan secretos y datos personales
ALLOWED: dict[tuple[Direction, ContentCheck], frozenset[ContentAction]] = {
    key: frozenset({A.BLOCK, A.LOG, A.OFF}) if key[1] in (C.JAILBREAK, C.CLASSIFIED) else frozenset(A)
    for key in DEFAULTS
}
NAMES = {C.JAILBREAK: "un intento de saltarse las instrucciones del modelo", C.CLASSIFIED: "marcas de clasificación",
         C.SECRET: "secretos (claves o credenciales)", C.PII: "datos personales"}


async def load_actions(ctx: PipelineContext, deps: Deps) -> dict[tuple[Direction, ContentCheck], ContentAction]:
    identity = ctx.identity
    async with deps.sessions() as db:
        rows = (await db.execute(select(ContentPolicy).where(
            ContentPolicy.deleted_at.is_(None),
            or_(and_(ContentPolicy.scope == PolicyScope.ORGANIZATION,
                     ContentPolicy.scope_id == identity.organization_id),
                and_(ContentPolicy.scope == PolicyScope.APPLICATION,
                     ContentPolicy.scope_id == identity.application_id))))).scalars().all()
    actions = dict(DEFAULTS)
    # Primero la organización y después la aplicación, que manda
    for row in sorted(rows, key=lambda r: r.scope == PolicyScope.APPLICATION):
        key = (row.direction, row.check_name)
        if key in ALLOWED and row.action in ALLOWED[key]:
            actions[key] = row.action
    return actions


def flag(ctx: PipelineContext, direction: Direction, check: ContentCheck, action: ContentAction,
         kinds: Counter, **extra) -> None:
    entry = {"direction": str(direction), "check": str(check), "action": str(action),
             "count": sum(kinds.values()), "kinds": dict(sorted(kinds.items())), **extra}
    ctx.security_flags.append(entry)
    log.warning("content.flagged", request_id=ctx.request_id, application=ctx.identity.application_slug, **entry)


def scan(ctx: PipelineContext, direction: Direction, checks: list[ContentCheck], slots: list[TextSlot]) -> None:
    """Busca en los textos las categorías pedidas y aplica su acción: bloquea, tacha o anota."""
    actions = ctx.content_actions
    active = {c for c in checks if actions.get((direction, c), A.OFF) != A.OFF}
    if not active:
        return
    redact_checks = {c for c in active if actions[(direction, c)] == A.REDACT}
    found: dict[str, Counter] = {}
    for slot in slots:
        findings = find(slot.value, active)
        if not findings:
            continue
        for check, kinds in tally(findings).items():
            found.setdefault(check, Counter()).update(kinds)
        if redact_checks:
            slot.set(redact(slot.value, [f for f in findings if f.check in redact_checks]))
    for check in checks:
        if check not in found:
            continue
        action = actions[(direction, check)]
        flag(ctx, direction, check, action, found[check])
        if action == A.BLOCK:
            where = "La petición contiene" if direction == Direction.INPUT else "La respuesta del modelo contenía"
            raise ContentBlocked(f"{where} {NAMES[check]} y la política de contenido no lo permite")


def header(ctx: PipelineContext) -> dict[str, str]:
    if not ctx.security_flags:
        return {}
    value = ",".join(f"{f['direction']}.{f['check']}={f['action']}" for f in ctx.security_flags)
    return {"X-Platia-Content-Flags": value}
