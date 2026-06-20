"""Tests for plugin-side Ed25519 auth (keygen, signing, token exchange, httpx flow).

No Pi, no network: the device API is a mock httpx transport.
"""

from __future__ import annotations

import asyncio
import base64
import os
import stat

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from autonomon.plugin_auth import (
    PluginTokenAuth,
    acquire_plugin_token,
    generate_keypair,
    load_private_key,
    public_key_pem,
    sign_nonce,
)

# ---------------------------------------------------------------------------
# Key management
# ---------------------------------------------------------------------------


def test_generate_keypair_writes_private_key_0600(tmp_path):
    path = str(tmp_path / "plugin.key")
    pub_pem = generate_keypair(path)

    assert os.path.exists(path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600
    assert "PUBLIC KEY" in pub_pem
    # The returned PEM is a real Ed25519 public key.
    key = serialization.load_pem_public_key(pub_pem.encode())
    assert isinstance(key, Ed25519PublicKey)


def test_generate_keypair_is_idempotent(tmp_path):
    path = str(tmp_path / "plugin.key")
    first = generate_keypair(path)
    second = generate_keypair(path)
    # Same key on disk → same public PEM, key not regenerated.
    assert first == second


def test_load_private_key_roundtrip(tmp_path):
    path = str(tmp_path / "plugin.key")
    pub_pem = generate_keypair(path)
    priv = load_private_key(path)
    assert isinstance(priv, Ed25519PrivateKey)
    assert public_key_pem(priv) == pub_pem


def test_load_private_key_rejects_non_ed25519(tmp_path):
    from cryptography.hazmat.primitives.asymmetric import rsa

    path = tmp_path / "rsa.key"
    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(pem)
    with pytest.raises(ValueError):
        load_private_key(str(path))


def test_sign_nonce_verifies_against_public_key():
    priv = Ed25519PrivateKey.generate()
    nonce = "challenge-123"
    sig = base64.b64decode(sign_nonce(priv, nonce))
    # Raises InvalidSignature if it does not verify.
    priv.public_key().verify(sig, nonce.encode())


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------


def _mock_device(priv: Ed25519PrivateKey, *, token: str = "device-jwt") -> httpx.MockTransport:
    """Mock nomothetic that runs the real challenge/token protocol checks."""
    pub = priv.public_key()
    state: dict[str, str | None] = {"nonce": None}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/plugin/challenge":
            state["nonce"] = "nonce-abc"
            return httpx.Response(
                200, json={"plugin": "autonomon", "nonce": "nonce-abc", "expires_in": 30.0}
            )
        if request.url.path == "/api/plugin/token":
            import json as _json

            body = _json.loads(request.content)
            sig = base64.b64decode(body["signature"])
            try:
                pub.verify(sig, body["nonce"].encode())
            except Exception:  # noqa: BLE001
                return httpx.Response(401, json={"detail": "authentication failed"})
            if body["nonce"] != state["nonce"]:
                return httpx.Response(401, json={"detail": "authentication failed"})
            return httpx.Response(
                200, json={"access_token": token, "token_type": "bearer", "expires_in": 3600}
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_acquire_plugin_token_happy_path(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    transport = _mock_device(priv, token="the-jwt")

    # Patch AsyncClient so acquire_plugin_token's internal client uses our transport.
    real_client = httpx.AsyncClient

    def _client(**kwargs):
        kwargs.pop("verify", None)
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)

    token = await acquire_plugin_token("https://device:8443", "autonomon", priv)
    assert token == "the-jwt"


# ---------------------------------------------------------------------------
# httpx.Auth flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plugin_token_auth_injects_bearer(monkeypatch):
    priv = Ed25519PrivateKey.generate()

    async def _fake_acquire(self):
        return "fresh-token"

    monkeypatch.setattr(PluginTokenAuth, "_acquire", _fake_acquire)

    auth = PluginTokenAuth("https://device:8443", "autonomon", priv)

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=auth) as client:
        resp = await client.get("https://device:8443/api/sensor/ultrasonic")
    assert resp.status_code == 200
    assert seen["auth"] == "Bearer fresh-token"


@pytest.mark.asyncio
async def test_plugin_token_auth_refreshes_on_401(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    tokens = iter(["stale-token", "renewed-token"])

    async def _fake_acquire(self):
        return next(tokens)

    monkeypatch.setattr(PluginTokenAuth, "_acquire", _fake_acquire)
    auth = PluginTokenAuth("https://device:8443", "autonomon", priv)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # First request (stale token) → 401; retry (renewed token) → 200.
        if request.headers.get("Authorization") == "Bearer stale-token":
            return httpx.Response(401, json={"detail": "expired"})
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=auth) as client:
        resp = await client.get("https://device:8443/api/sensor/ultrasonic")

    assert resp.status_code == 200
    assert calls["n"] == 2  # original + one retry after refresh


@pytest.mark.asyncio
async def test_plugin_token_auth_single_flight_on_cold_start(monkeypatch):
    # Concurrent requests on a cold (token=None) shared auth must acquire once.
    priv = Ed25519PrivateKey.generate()
    acquisitions = {"n": 0}

    async def _slow_acquire(self):
        acquisitions["n"] += 1
        await asyncio.sleep(0.02)  # widen the race window
        return "shared-token"

    monkeypatch.setattr(PluginTokenAuth, "_acquire", _slow_acquire)
    auth = PluginTokenAuth("https://device:8443", "autonomon", priv)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=auth) as client:
        await asyncio.gather(
            *(client.get("https://device:8443/api/sensor/ultrasonic") for _ in range(5))
        )

    assert acquisitions["n"] == 1  # single-flight: only one token fetched
