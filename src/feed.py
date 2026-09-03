"""HL WebSocket feed — reconnect-hardened async stream. Yields ('connected'|'msg'|'tick'|'drop', payload).
On the Tokyo box these endpoints resolve to HL's in-region infra (§AGENTS Deployment)."""
import asyncio
import json
import websockets


class HLFeed:
    def __init__(self, ws_url, universe):
        self.url, self.universe = ws_url, universe
        self._stop = False

    def stop(self):
        self._stop = True

    async def stream(self):
        while not self._stop:
            try:
                async with websockets.connect(self.url, ping_interval=20, max_queue=None) as ws:
                    for c in self.universe:
                        for ty in ("bbo", "l2Book", "trades"):     # bbo = the FAST touch (~78ms); l2Book = depth (~5.4s)
                            await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": ty, "coin": c}}))
                    yield ("connected", None)
                    while not self._stop:
                        try:
                            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                        except asyncio.TimeoutError:
                            yield ("tick", None); continue
                        yield ("msg", msg)
            except Exception as e:                                  # any WS error -> reconnect + resubscribe
                yield ("drop", f"{type(e).__name__}: {str(e)[:100]}")
                await asyncio.sleep(3)
