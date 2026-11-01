"""Etapa `security_input`: manipulación del modelo, marcas de clasificación y secretos en la entrada (cap. 19 y 20, ADR-012).

Carga la política de contenido de la petición (aplicación > organización > por defecto) y:

- **Manipulación (jailbreak)**: puntúa el texto de la persona (mensajes `user` y resultados
  de herramientas) y, desde `JAILBREAK_THRESHOLD`, bloquea (403) o lo anota.
- **Marcas de clasificación**: bloquea o anota un documento marcado como CONFIDENCIAL, etc.
- **Secretos**: los tacha antes de que salgan hacia el proveedor (por defecto), bloquea o anota.

Los datos personales van en la etapa siguiente, `filter`. Lo encontrado se anota en el
contexto (sin el texto) y la auditoría lo guarda en `security_flags`.
"""
from collections import Counter

from ..content import JAILBREAK_THRESHOLD, assistant_run, jailbreak_score, request_slots
from ..content_policy import flag, load_actions, scan
from ..errors import ContentBlocked
from ..models import ContentAction, ContentCheck, Direction
from .context import Deps, PipelineContext


async def run(ctx: PipelineContext, deps: Deps) -> None:
    ctx.content_actions = await load_actions(ctx, deps)
    action = ctx.content_actions[(Direction.INPUT, ContentCheck.JAILBREAK)]
    if action != ContentAction.OFF:
        texts = [s.value for s in request_slots(ctx.body) if s.role in ("user", "tool")]
        score, categories = jailbreak_score(texts, assistant_run(ctx.body))
        if score >= JAILBREAK_THRESHOLD:
            flag(ctx, Direction.INPUT, ContentCheck.JAILBREAK, action, Counter(categories), score=score)
            if action == ContentAction.BLOCK:
                raise ContentBlocked("La petición parece un intento de saltarse las instrucciones del modelo "
                                     "y la política de contenido no lo permite")
    scan(ctx, Direction.INPUT, [ContentCheck.CLASSIFIED, ContentCheck.SECRET], list(request_slots(ctx.body)))
