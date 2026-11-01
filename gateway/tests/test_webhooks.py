"""Firma, comprobación de URL y protección SSRF de los webhooks, sin base de datos (G12, ADR-016)."""
import asyncio

import pytest

from app import webhooks
from app.cli import _percentile
from app.events import WEBHOOK_EVENTS

PRIVATE = frozenset({"receptor.interno"})


def test_firma_y_comprobacion():
    body = '{"type":"quota.alert"}'
    header = webhooks.sign("plgw-whsec-abc", 1_700_000_000, body)
    assert header.startswith("t=1700000000,v1=") and len(header.split("v1=")[1]) == 64
    assert webhooks.verify("plgw-whsec-abc", header, body, now=1_700_000_100)
    assert not webhooks.verify("plgw-whsec-abc", header, body + " ", now=1_700_000_100)  # cuerpo cambiado
    assert not webhooks.verify("otro-secreto", header, body, now=1_700_000_100)
    assert not webhooks.verify("plgw-whsec-abc", header, body, now=1_700_000_400)  # más de 5 minutos
    assert not webhooks.verify("plgw-whsec-abc", "basura", body)
    assert not webhooks.verify("plgw-whsec-abc", "t=x,v1=y", body)


def test_secreto_nuevo():
    secret = webhooks.new_secret()
    assert secret.startswith("plgw-whsec-") and len(secret) == len("plgw-whsec-") + 64
    assert secret != webhooks.new_secret()


@pytest.mark.parametrize("url, error", [
    ("https://hooks.cotecnia.es/aviso", None),
    ("https://hooks.cotecnia.es:8443/aviso?x=1", None),
    ("http://receptor.interno:9000/hook", None),
    ("http://hooks.cotecnia.es/aviso", "Solo URLs https"),
    ("ftp://hooks.cotecnia.es/aviso", "Solo URLs https"),
    ("https://usuario:clave@hooks.cotecnia.es/", "usuario"),
    ("https:///sin-servidor", "nombre de servidor"),
    ("https://hooks.cotecnia.es:99999/", "URL no válida"),
    ("https://hooks.cotecnia.es/" + "x" * 2100, "demasiado larga"),
])
def test_check_url(url, error):
    if error is None:
        assert webhooks.check_url(url, PRIVATE)[0] in ("https", "http")
    else:
        with pytest.raises(webhooks.WebhookUrlError, match=error):
            webhooks.check_url(url, PRIVATE)


@pytest.mark.parametrize("address, public", [
    ("93.184.216.34", True), ("2606:2800:220:1:248:1893:25c8:1946", True),
    ("10.0.0.5", False), ("192.168.1.10", False), ("172.16.0.1", False), ("127.0.0.1", False),
    ("169.254.169.254", False),  # metadatos de la nube
    ("100.64.0.1", False), ("0.0.0.0", False), ("224.0.0.1", False), ("::1", False), ("fd00::1", False),
    ("::ffff:10.0.0.1", False),  # IPv4 privada escrita como IPv6
])
def test_direcciones_publicas(address, public):
    assert webhooks._public(address) is public


def _pinned(url, addresses, private=PRIVATE, monkeypatch=None):
    async def fake(host, port):
        if isinstance(addresses, Exception):
            raise addresses
        return addresses
    monkeypatch.setattr(webhooks, "resolve", fake)
    return asyncio.run(webhooks.pinned_request(url, private))


def test_conexion_fijada_a_la_direccion_comprobada(monkeypatch):
    url, headers, ext = _pinned("https://hooks.cotecnia.es/aviso?x=1", ["93.184.216.34"], monkeypatch=monkeypatch)
    assert url == "https://93.184.216.34/aviso?x=1"
    assert headers == {"Host": "hooks.cotecnia.es"} and ext == {"sni_hostname": "hooks.cotecnia.es"}
    url, headers, _ = _pinned("https://hooks.cotecnia.es:8443", ["2606:2800:220:1:248:1893:25c8:1946"],
                              monkeypatch=monkeypatch)
    assert url == "https://[2606:2800:220:1:248:1893:25c8:1946]:8443/" and headers == {"Host": "hooks.cotecnia.es:8443"}


def test_direccion_interna_rechazada(monkeypatch):
    with pytest.raises(webhooks.WebhookUrlError, match="dirección interna"):
        _pinned("https://hooks.cotecnia.es/", ["10.0.0.5"], monkeypatch=monkeypatch)
    # Basta una dirección interna entre varias (DNS que mezcla)
    with pytest.raises(webhooks.WebhookUrlError, match="dirección interna"):
        _pinned("https://hooks.cotecnia.es/", ["93.184.216.34", "127.0.0.1"], monkeypatch=monkeypatch)
    with pytest.raises(webhooks.WebhookUrlError, match="dirección interna"):
        _pinned("https://169.254.169.254/latest", [], monkeypatch=monkeypatch)
    with pytest.raises(webhooks.WebhookUrlError, match="no se resuelve"):
        _pinned("https://nadie.cotecnia.es/", OSError("NXDOMAIN"), monkeypatch=monkeypatch)
    with pytest.raises(webhooks.WebhookUrlError, match="no se resuelve"):
        _pinned("https://nadie.cotecnia.es/", [], monkeypatch=monkeypatch)


def test_receptor_interno_permitido(monkeypatch):
    url, headers, ext = _pinned("http://receptor.interno:9000/hook", ["172.18.0.4"], monkeypatch=monkeypatch)
    assert url == "http://172.18.0.4:9000/hook" and headers == {"Host": "receptor.interno:9000"} and ext == {}


def test_eventos_validos():
    assert webhooks.validate_events(["security.blocked", "quota.alert", "quota.alert"]) == ["quota.alert",
                                                                                            "security.blocked"]
    with pytest.raises(ValueError, match="request.finished"):
        webhooks.validate_events(["request.finished"])
    with pytest.raises(ValueError, match="ninguno"):
        webhooks.validate_events([])
    assert "request.finished" not in WEBHOOK_EVENTS


def test_percentil():
    values = list(range(1, 101))
    assert (_percentile(values, 50), _percentile(values, 95), _percentile(values, 99)) == (50, 95, 99)
    assert _percentile([7], 95) == 7
