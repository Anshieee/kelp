"""httpx adapter for the skill runtime's policy-checked egress transport."""

from __future__ import annotations

from typing import Callable, Mapping

import httpx

from .policy import EgressResponse

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def default_transport_factory() -> httpx.BaseTransport:
    """Create the production HTTPX transport; tests replace this factory."""

    return httpx.HTTPTransport()


def request(
    method: str,
    url: str,
    headers: Mapping[str, str] | None,
    body: bytes | None,
    timeout: float,
    *,
    transport_factory: Callable[[], httpx.BaseTransport] = default_transport_factory,
) -> EgressResponse:
    """Execute exactly one request through a replaceable httpx transport.

    Callers pass this function to ``context.egress``; that wrapper checks host
    and method before invoking it and activates the network guard only around
    this transport call. Tests can provide ``httpx.MockTransport`` without
    making any network connection.
    """

    transport = transport_factory()
    with httpx.Client(transport=transport, follow_redirects=False, timeout=timeout) as client:
        request = client.build_request(method, url, headers=headers, content=body)
        response = client.send(request, stream=True)
        try:
            chunks: list[bytes] = []
            size = 0
            truncated = False
            for chunk in response.iter_bytes():
                remaining = MAX_RESPONSE_BYTES + 1 - size
                chunks.append(chunk[:remaining])
                size += min(len(chunk), remaining)
                if size > MAX_RESPONSE_BYTES:
                    truncated = True
                    break
            return EgressResponse(
                status=response.status_code,
                headers=dict(response.headers),
                body=b"".join(chunks)[:MAX_RESPONSE_BYTES],
                url=str(response.url),
                truncated=truncated,
            )
        finally:
            response.close()
