# =========================================================
# LIVE PUSH FEED (SERVER-SENT EVENTS) — /api/v1/stream
# =========================================================

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, Query, Request
from starlette.responses import StreamingResponse

from services import live
from services.settings import settings

from .common import error


router = APIRouter(prefix="/api/v1", tags=["Live stream"])

KEEPALIVE_S = 15.0


def _sse(event, payload):

    return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"


@router.get("/stream", summary="Live push feed (Server-Sent Events)")
async def stream(
    request: Request,
    channels: Optional[str] = Query(
        None,
        description="Comma-separated subset: fleet, poses, v2v, events, alerts, autopilot, scenario. "
                    "Default: all.",
        examples=["fleet,poses,alerts"],
    ),
):
    """`text/event-stream`. Each SSE event's `data` is
    `{"channel", "seq", "ts", "data"}`; `seq` is monotonically increasing
    across the whole feed, so a client can detect a gap. On connect, and
    every `SSE_RESYNC_S` (default 30 s) afterwards, a full snapshot is sent
    for the "state" channels (fleet, poses, alerts, autopilot) as an
    `event: snapshot` with `{"channel", "data"}`; the "log" channels
    (events, v2v, scenario) only ever send deltas (polling `GET /api/events`,
    `GET /api/v1/v2v/messages` etc. is still there for a full read).

    When the stream drops, the client should fall back to polling
    `GET /api/fleet` and reconnect; `GET /api/fleet` keeps working for
    polling clients."""

    requested = [c.strip() for c in (channels or "").split(",") if c.strip()] or list(live.CHANNELS)
    unknown = [c for c in requested if c not in live.CHANNELS]

    if unknown:
        return error(
            f"Unknown channel(s): {', '.join(unknown)}. Allowed: {', '.join(live.CHANNELS)}.",
            400,
            "INVALID_CHANNEL",
        )

    client_id, queue = live.hub.subscribe(requested)

    async def generate():

        try:

            loop = asyncio.get_running_loop()
            last_resync = {ch: loop.time() for ch in requested if ch in live.STATE_CHANNELS}

            for channel in requested:
                yield _sse("snapshot", {"channel": channel, "data": live.snapshot(channel)})

            while True:

                if await request.is_disconnected():
                    break

                # Never wait longer than the next resync is due, so a quiet
                # connection still gets its periodic snapshot promptly; a
                # stale proxy still sees keep-alive traffic at least every
                # KEEPALIVE_S.
                wait = KEEPALIVE_S
                if last_resync:
                    soonest = min(settings.sse_resync_s - (loop.time() - when) for when in last_resync.values())
                    wait = max(0.05, min(wait, soonest))

                timed_out = False

                try:
                    message = await asyncio.wait_for(queue.get(), timeout=wait)
                    yield _sse(message["channel"], message)
                except asyncio.TimeoutError:
                    timed_out = True

                now = loop.time()
                resynced = False

                for channel, when in list(last_resync.items()):
                    if now - when >= settings.sse_resync_s:
                        last_resync[channel] = now
                        resynced = True
                        yield _sse("snapshot", {"channel": channel, "data": live.snapshot(channel)})

                if timed_out and not resynced:
                    yield ": keep-alive\n\n"

        finally:
            live.hub.unsubscribe(client_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",          # disable reverse-proxy buffering
        },
    )
