"""Etapa `filter`: datos personales en la entrada (cap. 19, ADR-012; RGPD).

Correo, DNI, NIE, IBAN, tarjeta y teléfono. Por defecto solo se anotan (cuántos y de qué
tipo, nunca el dato): una aplicación como PLATIA trabaja con pliegos y contratos que los
llevan por necesidad. Con la política en `redact` se tachan antes de salir hacia el
proveedor y con `block` la petición se rechaza.
"""
from ..content import request_slots
from ..content_policy import scan
from ..models import ContentCheck, Direction
from .context import Deps, PipelineContext


async def run(ctx: PipelineContext, deps: Deps) -> None:
    scan(ctx, Direction.INPUT, [ContentCheck.PII], list(request_slots(ctx.body)))
