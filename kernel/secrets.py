"""Per-tenant secret resolution (P4-1).

A resolver answers one question: *what is the value of secret ``name`` for the
tenant this resolver is bound to?* Values exist only for the duration of that
call. A :class:`SecretRef` is the shareable description of a secret — its name,
source, and rotation version — and never carries the value.

Two implementations exist in this build:

* :class:`EnvSecretResolver` reads one explicit environment mapping, or the
  process environment when constructed without one. The server always passes
  its authoritative mapping so boot configuration and resource lookup cannot
  disagree.
* :class:`TenantSecretResolver` reads a JSON file named by
  ``KELP_TENANT_SECRETS_FILE``. The file must be mode ``0600``: a group- or
  world-readable secrets file is refused before a single byte is parsed. Only
  the bound tenant's map is retained in memory, so a resolver for tenant A has
  no code path that can return tenant B's value.

Nothing here writes to SQLite, the audit log, metrics, or a logger. Helpers that
expose values (``secret_values``) exist solely to seed the process
:class:`openshell.audit.SecretScrubber`; callers must never serialize or log
their result.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, runtime_checkable

__all__ = [
    "EnvSecretResolver",
    "MissingSecret",
    "SecretFileError",
    "SecretRef",
    "SecretResolver",
    "TENANT_SECRETS_FILE_ENV",
    "TenantSecretResolver",
]

#: The environment variable naming the optional per-tenant secrets file. It is
#: re-exported by :mod:`kernel.config`; defined here so the file-backed resolver
#: and the service config share one spelling.
TENANT_SECRETS_FILE_ENV = "KELP_TENANT_SECRETS_FILE"

_REQUIRED_FILE_MODE = 0o600


@dataclass(frozen=True)
class SecretRef:
    """Metadata about a resolvable secret. Never a value.

    ``source`` is a stable label (``"env"``, ``"tenant-file"``), and ``version``
    is rotation metadata: the file modification time for a file-backed secret
    and ``None`` when the environment supplies the value.
    """

    name: str
    source: str
    version: int | None = None

    def as_metadata(self) -> dict[str, object]:
        """A JSON-safe description suitable for an audit row or admin view."""

        return {"name": self.name, "source": self.source, "version": self.version}

    def __repr__(self) -> str:
        return (
            "SecretRef("
            f"name={self.name!r}, "
            f"source={self.source!r}, "
            f"version={self.version!r})"
        )

    __str__ = __repr__


class MissingSecret(LookupError):
    """A resolver has no value for the requested secret.

    The message names the secret and, when the resolver is tenant-scoped, the
    tenant asking. It never includes a stored value or another tenant's name.
    """

    def __init__(self, name: str, *, tenant_id: str | None = None):
        self.name = name
        self.tenant_id = tenant_id
        scope = f" for tenant {tenant_id!r}" if tenant_id is not None else ""
        super().__init__(f"secret {name!r} is not configured{scope}")

    def __repr__(self) -> str:
        return (
            "MissingSecret("
            f"name={self.name!r}, tenant_id={self.tenant_id!r})"
        )


class SecretFileError(ValueError):
    """The per-tenant secrets file is missing, malformed, or too permissive.

    The message carries the path and the offending mode, never file contents.
    """


@runtime_checkable
class SecretResolver(Protocol):
    """Resolve one secret name to its current value."""

    def resolve(self, name: str) -> str:
        """Return the value or raise :class:`MissingSecret`."""
        ...


class EnvSecretResolver:
    """Resolve secrets from an explicitly supplied environment mapping.

    ``environ=None`` intentionally falls back to :data:`os.environ` for a
    process-wide resolver. Library callers and the server pass a mapping; the
    registry never constructs one implicitly.
    """

    def __init__(self, environ: Mapping[str, str] | None = None):
        self._environ = os.environ if environ is None else environ

    def resolve(self, name: str) -> str:
        raw = self._environ.get(name)
        if isinstance(raw, str) and raw.strip():
            return raw
        raise MissingSecret(name)

    def ref(self, name: str) -> SecretRef:
        self.resolve(name)
        return SecretRef(name=name, source="env")

    def __repr__(self) -> str:
        return f"EnvSecretResolver(entries={len(self._environ)})"

    __str__ = __repr__


class TenantSecretResolver:
    """Resolve one tenant's secrets from a private JSON file.

    Expected shape::

        {
          "owner": {"KELP_PROVIDER_API_KEY": "..."},
          "tenant-b": {"KELP_PROVIDER_API_KEY": "..."}
        }

    Only ``payload[tenant_id]`` is retained. The mode is checked with ``fstat``
    on the opened descriptor, so a ``0644`` file is never parsed even if its
    mode changes between a separate ``stat`` and the read. A missing tenant map
    behaves like an empty map: every name is :class:`MissingSecret`, which also
    makes an unknown tenant indistinguishable from a configured tenant with no
    keys.
    """

    def __init__(self, path: str | Path, *, tenant_id: str):
        self.path = Path(path)
        self.tenant_id = tenant_id
        raw, mtime_ns = self._read_private_file(self.path)
        self._version = int(mtime_ns)
        self._values = self._parse_values(raw, tenant_id, path=self.path)

    @staticmethod
    def _read_private_file(path: Path) -> tuple[str, int]:
        try:
            fd = os.open(path, os.O_RDONLY)
        except FileNotFoundError:
            raise SecretFileError(f"secret file {path} does not exist") from None
        except OSError as exc:
            raise SecretFileError(
                f"secret file {path} cannot be opened ({exc.__class__.__name__})"
            ) from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise SecretFileError(f"secret file {path} is not a regular file")
            mode = stat.S_IMODE(info.st_mode)
            if mode != _REQUIRED_FILE_MODE:
                raise SecretFileError(
                    f"secret file {path} must have mode 0600 (found {mode:04o})"
                )
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1  # ``fdopen`` owns the descriptor now.
                return handle.read(), int(info.st_mtime_ns)
        except UnicodeError:
            raise SecretFileError(f"secret file {path} is not valid UTF-8") from None
        except OSError as exc:
            raise SecretFileError(
                f"secret file {path} cannot be read ({exc.__class__.__name__})"
            ) from None
        finally:
            if fd >= 0:
                os.close(fd)

    @staticmethod
    def _parse_values(raw: str, tenant_id: str, *, path: Path) -> dict[str, str]:
        try:
            payload = json.loads(raw)
        except ValueError:
            raise SecretFileError(f"secret file {path} is not valid JSON") from None
        if not isinstance(payload, dict):
            raise SecretFileError(
                f"secret file {path} must contain a JSON object of tenant maps"
            )
        bucket = payload.get(tenant_id)
        if bucket is None:
            return {}
        if not isinstance(bucket, dict):
            raise SecretFileError(
                f"secret file {path} entry for tenant {tenant_id!r} must be an object"
            )
        values: dict[str, str] = {}
        for name, value in bucket.items():
            if not isinstance(name, str) or not name.strip():
                raise SecretFileError(
                    f"secret file {path} contains a blank secret name"
                )
            if not isinstance(value, str):
                raise SecretFileError(
                    f"secret file {path} entry {name!r} must be a string"
                )
            if value.strip():
                values[name] = value
        return values

    def resolve(self, name: str) -> str:
        value = self._values.get(name)
        if isinstance(value, str) and value.strip():
            return value
        raise MissingSecret(name, tenant_id=self.tenant_id)

    def ref(self, name: str) -> SecretRef:
        self.resolve(name)
        return SecretRef(
            name=name,
            source="tenant-file",
            version=self._version,
        )

    def secret_values(self) -> tuple[str, ...]:
        """This tenant's values, for constructing a redaction set only.

        The result must never be logged, serialized, or persisted. It exists so
        the server can seed the process scrubber with values it did not learn
        from its own environment.
        """

        return tuple(self._values.values())

    def __repr__(self) -> str:
        return (
            "TenantSecretResolver("
            f"path={str(self.path)!r}, "
            f"tenant_id={self.tenant_id!r}, "
            f"entries={len(self._values)})"
        )

    __str__ = __repr__
