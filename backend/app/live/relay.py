"""Relay worker activity events onto the existing authenticated WebSocket bus.

The worker is a separate process, so it cannot push to dashboard sockets itself.
The API polls the gateway's event cursor (only while someone is listening) and
broadcasts each NEW event to every authenticated client as ``live.event``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from backend.app.live import client

logger = logging.getLogger("airdropos.live.relay")


async def relay_loop(stop: asyncio.Event) -> None:
    from backend.app.api.v1.endpoints.ws import bus

    epoch: Optional[str] = None
    cursor: Optional[int] = None
    while not stop.is_set():
        try:
            if bus.client_count == 0:
                cursor = None  # nobody listening: forget the cursor so a later viewer is not flooded
                await asyncio.sleep(2)
                continue
            if cursor is None:
                head = await client.get_json("/events", {"limit": 1})
                epoch, cursor = head["epoch"], head["last_seq"]
            else:
                data = await client.get_json("/events", {"after": cursor, "epoch": epoch})
                if data["epoch"] != epoch:
                    epoch, cursor = data["epoch"], 0
                for event in data["events"]:
                    await bus.broadcast("live.event", event)
                    cursor = max(cursor, event["seq"])
        except client.GatewayUnavailable:
            cursor = None
            await asyncio.sleep(5)
            continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let the relay take the API down
            logger.debug("Live relay error: %s", exc)
            await asyncio.sleep(3)
            continue
        await asyncio.sleep(1)
