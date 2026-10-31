"""Ventanas, unidades, estimación de tokens y guiones de Redis de las cuotas (G7), sin base de datos."""
import asyncio
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import fakeredis

from app.limits import (ADMIT_LUA, SETTLE_LUA, counter_key, estimate_tokens, from_units, to_units, ttl_seconds,
                        window)
from app.models import LimitMetric, LimitPeriod, LimitScope
from app.pipeline.quota import _applicable


def test_ventanas_naturales_en_utc():
    now = datetime(2026, 12, 31, 23, 59, 30, 500)
    minute, day, month = (window(p, now) for p in (LimitPeriod.MINUTE, LimitPeriod.DAY, LimitPeriod.MONTH))
    assert (minute.stamp, minute.end) == ("202612312359", datetime(2027, 1, 1, 0, 0))
    assert (day.stamp, day.start, day.end) == ("20261231", datetime(2026, 12, 31), datetime(2027, 1, 1))
    assert (month.stamp, month.start, month.end) == ("202612", datetime(2026, 12, 1), datetime(2027, 1, 1))
    assert window(LimitPeriod.MONTH, datetime(2026, 2, 10)).end == datetime(2026, 3, 1)
    live = window(LimitPeriod.NONE, now)
    assert (live.stamp, live.start, live.end) == ("live", None, None)


def test_caducidad_hasta_el_final_mas_margen():
    now = datetime(2026, 10, 31, 23, 0, 0)
    assert ttl_seconds(window(LimitPeriod.DAY, now), now, 99) == 3600 + 3600
    assert ttl_seconds(window(LimitPeriod.NONE, now), now, 99) == 99


def test_claves_y_unidades():
    win = window(LimitPeriod.DAY, datetime(2026, 10, 31))
    assert counter_key(LimitMetric.COST_USD, LimitScope.END_USER, 3, "7", win) == "gw:q:cost_usd:end_user:3:7:20261031"
    assert counter_key(LimitMetric.REQUESTS, LimitScope.TEAM, 2, None, win) == "gw:q:requests:team:2:20261031"
    assert to_units(LimitMetric.COST_USD, Decimal("0.0111")) == 11100
    assert to_units(LimitMetric.TOKENS, Decimal("10.6")) == 11
    assert from_units(LimitMetric.COST_USD, 11100) == Decimal("0.0111")
    assert from_units(LimitMetric.REQUESTS, 5) == 5


def test_estimacion_de_tokens():
    body = {"system": "x" * 40, "messages": [{"role": "user", "content": "y" * 400}], "max_tokens": 100}
    assert 100 + 100 < estimate_tokens(body, 8000) < 100 + 130
    assert estimate_tokens({"messages": []}, 8000) == 8000 + 1  # sin max_tokens: el del modelo
    assert estimate_tokens({"messages": [], "max_completion_tokens": 50}, None) == 51
    assert estimate_tokens({"messages": [], "max_tokens": "mucho"}, None) == 4096 + 1


def test_admision_todo_o_nada_y_ajuste():
    async def scenario():
        redis = fakeredis.FakeAsyncRedis()
        assert await redis.eval(ADMIT_LUA, 2, "a", "b", 2, 1, 60, 10, 5, 60) == [1, 5]
        assert await redis.eval(ADMIT_LUA, 2, "a", "b", 2, 1, 60, 10, 5, 60) == [2, 10]
        # "b" ya está en 10: se rechaza y "a" no sube
        assert await redis.eval(ADMIT_LUA, 2, "a", "b", 5, 1, 60, 10, 5, 60) == 2
        assert int(await redis.get("a")) == 2
        assert 0 < await redis.ttl("a") <= 60
        # El ajuste no recrea contadores perdidos y no baja de cero
        assert await redis.eval(SETTLE_LUA, 3, "a", "b", "perdido", -5, 3, 7) == [0, 13, -1]
        assert await redis.exists("perdido") == 0
        assert 0 < await redis.ttl("a") <= 60
    asyncio.run(scenario())


def test_el_limite_de_una_persona_sustituye_al_general():
    def row(subject, metric=LimitMetric.REQUESTS, period=LimitPeriod.DAY, scope=LimitScope.END_USER):
        return SimpleNamespace(scope=scope, subject=subject, metric=metric, period=period)
    general, personal = row(None), row("ana")
    other_metric = row(None, metric=LimitMetric.COST_USD)
    app = row(None, scope=LimitScope.APPLICATION)
    assert _applicable([general, personal, other_metric, app], "ana") == [personal, other_metric, app]
    assert _applicable([general, other_metric], "luis") == [general, other_metric]
