"""`GET /v1/realtime` — the live alarm socket (spec §16.4).

Authentication is the awkward part of WebSockets: a browser cannot set an
`Authorization` header on the handshake, so the access token may also arrive as a
query parameter. That is accepted here, with two mitigations that matter: only the
short-lived *access* token is accepted (never the refresh token), and the token is
never logged — the JSON formatter only ever sees the tenant id.

The socket is read-only apart from a client `ping`. Acknowledging an alarm goes
through the REST endpoint, where permissions and the audit trail already live; adding
a second write path would mean a second place to get authorisation wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from app.core.config import get_settings
from app.core.rbac import Permission, Role, permissions_for
from app.modules.auth.tokens import TokenError, decode_access_token
from app.modules.realtime.hub import frames, hub

log = logging.getLogger(__name__)

router = APIRouter(tags=["realtime"])

#: Closing codes the client distinguishes: bad token means "log in again", forbidden
#: means "this account is not for guarding", so the app can show the right message.
CLOSE_UNAUTHORIZED = status.WS_1008_POLICY_VIOLATION


@router.websocket("/v1/realtime")
async def realtime(
    websocket: WebSocket,
    access_token: str | None = Query(default=None),
) -> None:
    token = access_token or _bearer_from_header(websocket)
    if not token:
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="authentication required")
        return

    settings = get_settings()
    try:
        claims = decode_access_token(token, settings.jwt_secret)
    except TokenError as exc:
        log.info("realtime handshake rejected: %s", exc)
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="invalid token")
        return

    try:
        role = Role(claims.get("role", ""))
    except ValueError:
        role = Role.VIEWER
    if Permission.ALARMS_VIEW not in permissions_for(role):
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="not permitted")
        return

    from uuid import UUID

    tenant_id = UUID(claims["tid"])
    user_id = UUID(claims["sub"])

    await websocket.accept()

    with hub.subscribe(tenant_id=tenant_id, user_id=user_id) as subscriber:
        await websocket.send_json(
            {
                "type": "hello",
                "data": {
                    # The client refetches from /v1/alarms on reconnect rather than
                    # trusting the socket to have replayed what it missed.
                    "resume": "refetch",
                    "heartbeat_seconds": 25,
                    "subscriber_id": str(subscriber.id),
                },
            }
        )

        reader = asyncio.create_task(_drain(websocket))
        try:
            async for frame in frames(subscriber):
                if reader.done():
                    break
                await websocket.send_json(frame)
        except WebSocketDisconnect:
            pass
        except RuntimeError as exc:  # socket closed underneath us mid-send
            log.info("realtime send failed for tenant %s: %s", tenant_id, exc)
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader


async def _drain(websocket: WebSocket) -> None:
    """Consume client frames so a disconnect is noticed promptly.

    Without this the server only learns the phone is gone when it next tries to send,
    which on a quiet night could be half a minute after the guard walked out of range.
    """
    while True:
        message = await websocket.receive_text()
        if message.strip().lower() in ('{"type":"ping"}', "ping"):
            await websocket.send_json({"type": "pong"})


def _bearer_from_header(websocket: WebSocket) -> str | None:
    header = websocket.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


__all__ = ["router"]
