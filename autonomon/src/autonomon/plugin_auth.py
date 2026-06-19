"""Plugin-side Ed25519 challenge-response auth against nomothetic.

autonomon authenticates to the device's nomothetic API without any secret token
on disk. Instead it holds an Ed25519 *private key* (generated on-device during
deploy) and proves possession of it at runtime: it fetches a server nonce, signs
it, and exchanges the signature for a short-lived device JWT. See nomothetic ADR-019 (and
the nomothetic ``plugin_auth`` module) for the protocol and threat model.

This module provides:

* :func:`generate_keypair` / :func:`load_private_key` / :func:`public_key_pem` —
  on-device key management used by the deploy script.
* :func:`acquire_plugin_token` — the runtime challenge → sign → token exchange.
* :class:`PluginTokenAuth` — an :class:`httpx.Auth` that injects the token and
  transparently re-acquires it on a ``401`` (so a long-running pipeline survives
  token expiry without any per-layer changes).
* :func:`main` — a small CLI (``generate-key`` / ``register``) the deploy script
  calls on the Pi.

Only the private key ever touches disk; the JWT lives in memory for the life of
the process.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import stat
import sys
import tempfile
from collections.abc import AsyncGenerator

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_DEFAULT_TIMEOUT_S = 5.0


# ---------------------------------------------------------------------------
# Key management (used by the deploy script, on-device)
# ---------------------------------------------------------------------------


def public_key_pem(private_key: Ed25519PrivateKey) -> str:
    """Return the PEM-encoded public key for *private_key*."""
    return (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )


def load_private_key(path: str) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from a PEM file.

    Parameters
    ----------
    path : str
        Path to the PEM-encoded private key.

    Returns
    -------
    Ed25519PrivateKey

    Raises
    ------
    ValueError
        If the file does not contain an Ed25519 private key.
    """
    with open(path, "rb") as fh:
        key = serialization.load_pem_private_key(fh.read(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"{path} does not contain an Ed25519 private key")
    return key


def generate_keypair(path: str) -> str:
    """Generate an Ed25519 private key at *path* (``0600``) and return its public PEM.

    Idempotent: if *path* already holds a valid Ed25519 private key, it is loaded
    rather than overwritten, so re-running deploy keeps the registered identity
    stable. The private key is written atomically with owner-only permissions.

    Parameters
    ----------
    path : str
        Destination path for the private key PEM.

    Returns
    -------
    str
        The PEM-encoded public key (to register with nomothetic).
    """
    if os.path.exists(path):
        return public_key_pem(load_private_key(path))

    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    target_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target_dir, prefix=".plugin_key_")
    try:
        os.write(fd, pem)
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        os.close(fd)
        fd = -1
        os.rename(tmp, path)
        tmp = ""
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    return public_key_pem(private_key)


# ---------------------------------------------------------------------------
# Runtime token acquisition
# ---------------------------------------------------------------------------


def sign_nonce(private_key: Ed25519PrivateKey, nonce: str) -> str:
    """Return the base64-encoded Ed25519 signature over *nonce* (UTF-8)."""
    return base64.b64encode(private_key.sign(nonce.encode("utf-8"))).decode("ascii")


async def acquire_plugin_token(
    base_url: str,
    plugin: str,
    private_key: Ed25519PrivateKey,
    *,
    verify: bool = False,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> str:
    """Run the challenge → sign → token exchange and return a device JWT.

    Uses its own short-lived, unauthenticated client (the challenge/token
    endpoints are the bootstrap, so they take no bearer token) — keeping it
    separate from the authenticated pipeline client avoids any auth recursion.

    Parameters
    ----------
    base_url : str
        Base URL of the device's nomothetic API.
    plugin : str
        Registered plugin name (e.g. ``"autonomon"``).
    private_key : Ed25519PrivateKey
        The key whose public half is registered with nomothetic.
    verify : bool, optional
        TLS verification. Defaults to ``False`` (self-signed device certs).
    timeout : float, optional
        Per-request timeout in seconds.

    Returns
    -------
    str
        A short-lived device JWT.

    Raises
    ------
    httpx.HTTPStatusError
        If the challenge or token request fails (e.g. unregistered key).
    """
    async with httpx.AsyncClient(base_url=base_url, verify=verify, timeout=timeout) as client:
        ch = await client.get("/api/plugin/challenge", params={"plugin": plugin})
        ch.raise_for_status()
        nonce = ch.json()["nonce"]

        resp = await client.post(
            "/api/plugin/token",
            json={"plugin": plugin, "nonce": nonce, "signature": sign_nonce(private_key, nonce)},
        )
        resp.raise_for_status()
        token: str = resp.json()["access_token"]
        return token


class PluginTokenAuth(httpx.Auth):
    """httpx auth flow that acquires and refreshes a plugin device JWT.

    On the first request it acquires a token via :func:`acquire_plugin_token`;
    on any ``401`` it re-acquires once and retries. This keeps the perception and
    action layers oblivious to auth — they just use the client, and tokens are
    fetched and refreshed transparently.

    Parameters
    ----------
    base_url : str
        Base URL of the device's nomothetic API.
    plugin : str
        Registered plugin name.
    private_key : Ed25519PrivateKey
        The plugin's private key.
    verify : bool, optional
        TLS verification for the token-exchange client. Defaults to ``False``.
    """

    def __init__(
        self,
        base_url: str,
        plugin: str,
        private_key: Ed25519PrivateKey,
        *,
        verify: bool = False,
    ) -> None:
        self._base_url = base_url
        self._plugin = plugin
        self._private_key = private_key
        self._verify = verify
        self._token: str | None = None
        # Created lazily inside the running loop (py39-safe; avoids binding to a
        # loop at construction time).
        self._lock: asyncio.Lock | None = None

    async def _acquire(self) -> str:
        return await acquire_plugin_token(
            self._base_url, self._plugin, self._private_key, verify=self._verify
        )

    async def _refresh(self, stale: str | None) -> str:
        """Acquire a token, single-flight across concurrent callers.

        The pipeline shares one client across concurrent perception and action
        tasks, so a token expiry triggers many simultaneous 401s. Only the first
        caller through the lock re-acquires; the rest reuse the token it fetched
        (detected by the token having changed from the *stale* value they saw).
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._token == stale:
                self._token = await self._acquire()
            return self._token  # type: ignore[return-value]  # set above or by another caller

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        token = self._token
        if token is None:
            token = await self._refresh(None)
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            token = await self._refresh(token)
            request.headers["Authorization"] = f"Bearer {token}"
            yield request


# ---------------------------------------------------------------------------
# Deploy-time CLI: `python -m autonomon.plugin_auth ...`
# ---------------------------------------------------------------------------


def _cmd_generate_key(args: argparse.Namespace) -> int:
    """Generate (or load) the private key and print its public PEM to stdout."""
    pub_pem = generate_keypair(args.path)
    sys.stdout.write(pub_pem)
    if not pub_pem.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _cmd_register(args: argparse.Namespace) -> int:
    """Register the key's public half with nomothetic (localhost, on-device)."""
    private_key = load_private_key(args.key)
    pub_pem = public_key_pem(private_key)
    with httpx.Client(
        base_url=args.device_url, verify=args.verify, timeout=_DEFAULT_TIMEOUT_S
    ) as c:
        resp = c.post(
            "/api/plugin/register",
            json={"plugin": args.plugin, "public_key": pub_pem},
        )
    if resp.status_code != 200:
        sys.stderr.write(f"registration failed (HTTP {resp.status_code}): {resp.text}\n")
        return 1
    sys.stdout.write(f"{args.plugin}: {resp.json().get('status')}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for on-device key management during deploy.

    Subcommands
    -----------
    generate-key <path>
        Generate (idempotently) an Ed25519 private key and print its public PEM.
    register --device-url URL --plugin NAME --key PATH
        Register the key's public half with nomothetic (localhost only).
    """
    parser = argparse.ArgumentParser(prog="autonomon.plugin_auth")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate-key", help="Generate an Ed25519 key; print public PEM")
    gen.add_argument("path", help="Path to write/read the private key PEM")
    gen.set_defaults(func=_cmd_generate_key)

    reg = sub.add_parser("register", help="Register the public key with nomothetic")
    reg.add_argument(
        "--device-url", required=True, help="nomothetic base URL (localhost on device)"
    )
    reg.add_argument("--plugin", default="autonomon", help="Plugin name (default: autonomon)")
    reg.add_argument("--key", required=True, help="Path to the private key PEM")
    reg.add_argument(
        "--verify",
        action="store_true",
        help="Verify TLS (default off for self-signed device certs)",
    )
    reg.set_defaults(func=_cmd_register)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
