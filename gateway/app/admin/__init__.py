"""API del panel de administración (G13, ADR-017): todo bajo `/admin`, solo para administradores."""
from fastapi import APIRouter

from . import applications, catalog, hooks, observe, people, rules

router = APIRouter(prefix="/admin")
for module in (applications, people, rules, observe, hooks, catalog):
    router.include_router(module.router)

__all__ = ["router"]
