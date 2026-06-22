"""Publish autonomon's routine catalogue to a file for nomothetic to read.

Decoupling (ADR-005): nomothetic (comms/service) and autonomon (autonomy) are
standalone projects that run from **separate virtualenvs**; neither imports the
other. Instead autonomon writes its catalogue — the public ``nomon_manifest``
(routine names, parameter schemas, version) plus the absolute path to its own
``nomon-autonomon`` CLI — to a JSON file at a shared absolute path, and nomothetic
reads that file to both list routines (``GET /api/routines/available``) and launch
them. The path defaults to ``/var/lib/nomon/routine_catalog.json`` and is shared
via ``NOMON_ROUTINE_CATALOG_PATH`` (set identically in both repos' ``.env.device``).

This is invoked once at deploy time (``python -m autonomon.routines.publish``); the
catalogue only changes when autonomon is redeployed, so a static file is sufficient
and keeps the two services loosely coupled through a filesystem contract.
"""

from __future__ import annotations

import json
import os
import sys
import sysconfig
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autonomon.routines import nomon_manifest

# Shared default path. MUST match nomothetic's ``routine_catalog.DEFAULT_CATALOG_PATH``
# and the ``NOMON_ROUTINE_CATALOG_PATH`` default in both repos' ``.env.device`` files.
DEFAULT_CATALOG_PATH = "/var/lib/nomon/routine_catalog.json"


def _autonomon_bin() -> str:
    """Return the absolute path to this venv's ``nomon-autonomon`` console script.

    This is how nomothetic launches a routine: it execs this binary (in autonomon's
    own venv) as a subprocess. The path comes from this interpreter's *scripts*
    directory (:func:`sysconfig.get_path`) — i.e. the venv's ``bin/`` — which is
    exactly where ``uv``/pip install console scripts.

    Do **not** derive this from ``Path(sys.executable).resolve()``: a ``uv``-created
    venv's ``bin/python`` is a symlink to the base interpreter, so resolving it
    escapes the venv (e.g. to ``/usr/bin``), where the console script does not
    exist — which is exactly the bad path that breaks the launch.

    Returns
    -------
    str
        Absolute path to the ``nomon-autonomon`` script in this venv's scripts dir.
    """
    return str(Path(sysconfig.get_path("scripts")) / "nomon-autonomon")


def build_catalog() -> dict[str, Any]:
    """Project the in-process manifest into the on-disk catalogue document.

    Returns
    -------
    dict
        ``{"name", "version", "routines", "params_schema", "autonomon_bin",
        "published_at"}``. ``autonomon_bin`` and ``published_at`` are added here;
        the rest mirror :data:`autonomon.routines.nomon_manifest`.
    """
    routines = nomon_manifest.get("routines", [])
    params_schema = nomon_manifest.get("params_schema", {})
    return {
        "name": nomon_manifest.get("name", "autonomon"),
        "version": nomon_manifest.get("version"),
        "routines": list(routines) if isinstance(routines, (list, tuple)) else [],
        "params_schema": dict(params_schema) if isinstance(params_schema, Mapping) else {},
        "autonomon_bin": _autonomon_bin(),
        "published_at": datetime.now(timezone.utc).isoformat(),
    }


def publish(path: str | os.PathLike[str]) -> Path:
    """Write the catalogue document to *path* atomically.

    Parameters
    ----------
    path : str or os.PathLike
        Destination file. Parent directories are created if missing. The write is
        atomic (write-to-temp then ``rename``) so a concurrent reader in nomothetic
        never sees a half-written file.

    Returns
    -------
    pathlib.Path
        The path that was written.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    document = json.dumps(build_catalog(), indent=2, sort_keys=True) + "\n"
    tmp = target.with_name(f"{target.name}.tmp")
    tmp.write_text(document, encoding="utf-8")
    tmp.replace(target)  # atomic on POSIX (same directory)
    return target


def main(argv: list[str] | None = None) -> int:
    """Console entry point: publish the catalogue and report what was written.

    Parameters
    ----------
    argv : list of str, optional
        Command-line arguments. ``argv[0]``, when present, overrides the output
        path; otherwise ``NOMON_ROUTINE_CATALOG_PATH`` (or the built-in default)
        is used.

    Returns
    -------
    int
        Process exit code (always ``0``; write errors propagate as exceptions so
        the deploy script fails loudly).
    """
    argv = sys.argv[1:] if argv is None else argv
    path = argv[0] if argv else os.environ.get("NOMON_ROUTINE_CATALOG_PATH") or DEFAULT_CATALOG_PATH
    written = publish(path)
    routines = build_catalog()["routines"]
    print(f"published {len(routines)} routine(s) to {written}: {routines}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
