"""Tenant identity for the control layer (P2-1).

There is exactly one admitted tenant today, :data:`OWNER_TENANT_ID`. A tenant
id is a URL-safe slug, not free text, so a future second tenant cannot introduce
a value that would need escaping in a URL, a log line, or an admin filter.

This module is deliberately free of storage, HTTP, and provider imports: it is
the small vocabulary that the store, the service gates, and the tests share.
Threading a :class:`TenantContext` through the service and API is P2-2's job;
P2-1 only makes the tenant explicit in storage and at the boundary.
"""

from __future__ import annotations

import contextvars
import hmac
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final, Iterator

__all__ = [
    "ACTOR_IDS",
    "OWNER_TENANT_ID",
    "ROLES",
    "SHARE_TOKEN_MODEL",
    "SURFACES",
    "TENANT_MISMATCH",
    "TENANT_SLUG_PATTERN",
    "Principal",
    "StaticTenantResolver",
    "TenantContext",
    "TenantIdError",
    "TenantMismatchError",
    "bind_tenant",
    "current_tenant",
    "ensure_tenant",
    "is_valid_tenant_id",
    "share_read_tenant",
    "tenant_for",
    "validate_tenant_id",
]

#: The only tenant this deployment admits. Every row that predates P2-1 is
#: backfilled to this id.
OWNER_TENANT_ID: Final = "owner"

#: ADR 0003 §3: one DNS-label-shaped slug. Lowercase ASCII digits and inner
#: hyphens, starting and ending alphanumeric, at most 63 characters.
TENANT_SLUG_PATTERN: Final = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
_TENANT_SLUG_RE = re.compile(TENANT_SLUG_PATTERN)

#: The frozen contract's vocabularies. They are documented here so a caller
#: cannot silently invent a new actor or surface; only tenant ids are validated
#: in P2-1, because the other values are already closed in their own layers.
ROLES: Final = ("owner",)
ACTOR_IDS: Final = ("owner", "system", "scheduler", "plugin", "share")
SURFACES: Final = ("web", "desktop", "mobile", "telegram", "cli", "scheduler")

#: The contract's typed tenant failure.
TENANT_MISMATCH: Final = "tenant_mismatch"

#: How a public share read resolves its tenant.
#:
#: ``tenant_scoped_token`` means the link carries the tenant that created it:
#: the lookup key is the token hash *and* the tenant, and a row belonging to a
#: different tenant is indistinguishable from an unknown link (404). That is
#: the same fails-closed shape the owner-scoped store lookup already has, and
#: it keeps a public reader from learning whether another tenant exists.
#:
#: The alternative (a tenant-neutral capability looked up unscoped, then
#: checked) needs a store-level unscoped live lookup. P2-2 does not add that,
#: so the documented choice is the scoped token below.
SHARE_TOKEN_MODEL: Final = "tenant_scoped_token"


class TenantIdError(ValueError):
    """A tenant id is not a valid slug."""


class TenantMismatchError(Exception):
    """A caller named a tenant this boundary does not serve.

    It carries the contract's error code (``tenant_mismatch``) and an HTTP
    status so a later API layer can map it without guessing. The message names
    no tenant, so an error body cannot become a tenant oracle.
    """

    code: Final = TENANT_MISMATCH
    status: Final = 403

    def __init__(self, message: str | None = None):
        super().__init__(
            message
            or "The request names a tenant this service does not serve."
        )


def is_valid_tenant_id(value: object) -> bool:
    """True only for a string that matches the frozen slug rule."""

    return isinstance(value, str) and bool(_TENANT_SLUG_RE.fullmatch(value))


def validate_tenant_id(value: object) -> str:
    """Return ``value`` unchanged when it is a slug, else raise.

    Validation happens on write paths. A read path may receive an unknown but
    well-formed id; it simply matches no rows, which is the correct empty answer
    and avoids turning an invalid id into an existence oracle.
    """

    if not is_valid_tenant_id(value):
        raise TenantIdError(
            "tenant_id must match "
            f"{TENANT_SLUG_PATTERN} (lowercase letters, digits, and inner hyphens)."
        )
    return str(value)


def ensure_tenant(tenant_id: str, expected: str = OWNER_TENANT_ID) -> str:
    """Raise :class:`TenantMismatchError` unless the two ids are equal."""

    if tenant_id != expected:
        raise TenantMismatchError()
    return tenant_id


@dataclass(frozen=True)
class TenantContext:
    """The four values every gate needs before doing work.

    Field order is part of the frozen contract: ``tenant_id``, ``actor_id``,
    ``role``, ``surface``. ``TenantContext`` is also the principal for the
    control layer's purposes; P0 has no separate account table.
    """

    tenant_id: str = OWNER_TENANT_ID
    actor_id: str = "owner"
    role: str = "owner"
    surface: str = "web"

    def __post_init__(self) -> None:
        validate_tenant_id(self.tenant_id)


#: The contract calls the same shape a principal. Keep both names so a caller
#: can use the wording its own layer prefers without a second type.
Principal = TenantContext

#: Request-scoped context. The API binds the authenticated principal here so a
#: route module that does not take a ``TenantContext`` argument (the existing
#: router split) still resolves the same tenant its service call uses. The
#: default is ``None`` rather than a shared owner object so a test can tell
#: "nothing bound" from "explicitly bound to owner".
_REQUEST_TENANT: Final[contextvars.ContextVar[TenantContext | None]] = (
    contextvars.ContextVar("kelp_request_tenant", default=None)
)


def current_tenant() -> TenantContext:
    """The tenant bound to this request/task, or the owner when none is bound."""

    return _REQUEST_TENANT.get() or TenantContext()


@contextmanager
def bind_tenant(context: TenantContext) -> Iterator[TenantContext]:
    """Bind ``context`` for the current request and every task it creates.

    ``asyncio.create_task`` copies the context, so a turn started inside the
    request keeps the same tenant. The token is reset in ``finally`` even when
    the route raises.
    """

    token = _REQUEST_TENANT.set(context)
    try:
        yield context
    finally:
        _REQUEST_TENANT.reset(token)


def tenant_for(tenant: TenantContext | str | None = None) -> TenantContext:
    """Coerce an explicit tenant argument, else use the bound one.

    ``None`` means "whatever this request resolved", so existing callers that
    pass no tenant keep the owner default through :func:`current_tenant`.
    A bare string is accepted for tests and headless callers and keeps the
    bound actor/role/surface.
    """

    if tenant is None:
        return current_tenant()
    if isinstance(tenant, TenantContext):
        return tenant
    bound = current_tenant()
    return TenantContext(
        tenant_id=validate_tenant_id(tenant),
        actor_id=bound.actor_id,
        role=bound.role,
        surface=bound.surface,
    )


def share_read_tenant(
    record_tenant_id: str, *, served_tenant_id: str = OWNER_TENANT_ID
) -> str:
    """The tenant a public share read may resolve, or a typed mismatch.

    The store already scopes ``share_get`` by tenant, so this helper states the
    decision next to the model constant instead of leaving it implicit in a
    default argument. A row from another tenant is treated as unknown.
    """

    return ensure_tenant(validate_tenant_id(record_tenant_id), served_tenant_id)


class StaticTenantResolver:
    """Map the current owner credential to the owner tenant.

    With auth enabled, ``owner_token`` is the process's root bearer token and
    only an exact match resolves; anything else is a typed
    :class:`TenantMismatchError`. With auth disabled (``owner_token is None``)
    every caller is the single owner, which preserves the current deployment.

    The token is held for comparison only: it is never logged, rendered in a
    ``repr``, or written to a context.
    """

    def __init__(
        self,
        owner_token: str | None = None,
        *,
        tenant_id: str = OWNER_TENANT_ID,
        actor_id: str = "owner",
        role: str = "owner",
        surface: str = "web",
    ):
        self._owner_token = owner_token
        self._tenant_id = validate_tenant_id(tenant_id)
        self._actor_id = actor_id
        self._role = role
        self._surface = surface

    def resolve(
        self,
        token: str | None = None,
        *,
        actor_id: str | None = None,
        role: str | None = None,
        surface: str | None = None,
    ) -> TenantContext:
        """Return the owner context for the owner token, or raise.

        ``actor_id``, ``role``, and ``surface`` let a non-human caller (a
        scheduler, a plugin, a share viewer) keep its own controlled identity
        without inventing a second tenant.
        """

        if self._owner_token is not None:
            presented = "" if token is None else str(token)
            if not hmac.compare_digest(presented, self._owner_token):
                raise TenantMismatchError()
        return self.context_for(
            actor_id=actor_id, role=role, surface=surface
        )

    def context_for(
        self,
        *,
        actor_id: str | None = None,
        role: str | None = None,
        surface: str | None = None,
    ) -> TenantContext:
        """Map an already-authenticated credential to this resolver's tenant.

        The caller has just compared the credential with the process's current
        owner token (which a test or a hot reload may have replaced), so this
        does not repeat the comparison. It still never invents a tenant: the id
        is the one this resolver was built with.
        """

        return TenantContext(
            tenant_id=self._tenant_id,
            actor_id=self._actor_id if actor_id is None else actor_id,
            role=self._role if role is None else role,
            surface=self._surface if surface is None else surface,
        )

    def __repr__(self) -> str:
        return f"StaticTenantResolver(tenant_id={self._tenant_id!r})"
