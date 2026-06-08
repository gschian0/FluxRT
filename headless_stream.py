import asyncio
import contextlib
import json
from fractions import Fraction
from typing import Optional

import aiohttp_cors
import av
import numpy as np
import zmq
import zmq.asyncio
from aiohttp import web
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer

SUB_ADDR = "tcp://127.0.0.1:5555"


class LatestFrameHub:
    def __init__(self) -> None:
        self._frame: Optional[np.ndarray] = None
        self._version = 0
        self._cond = asyncio.Condition()

    async def set(self, frame: np.ndarray) -> None:
        async with self._cond:
            self._frame = frame
            self._version += 1
            self._cond.notify_all()

    async def wait_next(self, last_version: int) -> tuple[np.ndarray, int]:
        async with self._cond:
            while self._version == last_version or self._frame is None:
                await self._cond.wait()
            return self._frame, self._version


class ZMQVideoTrack(MediaStreamTrack):
    kind = "video"

    def __init__(self, hub: LatestFrameHub) -> None:
        super().__init__()
        self.hub = hub
        self._version = 0

    async def recv(self) -> av.VideoFrame:
        frame_nd, self._version = await self.hub.wait_next(self._version)
        pts, time_base = await self.next_timestamp()
        video = av.VideoFrame.from_ndarray(frame_nd, format="bgr24")
        video.pts = pts
        video.time_base = time_base if time_base is not None else Fraction(1, 30)
        return video


async def zmq_subscriber(hub: LatestFrameHub) -> None:
    context = zmq.asyncio.Context.instance()
    sock = context.socket(zmq.SUB)
    sock.setsockopt(zmq.CONFLATE, 1)
    sock.setsockopt(zmq.SUBSCRIBE, b"")
    sock.connect(SUB_ADDR)
    print(f"[ZMQ] SUB connected to {SUB_ADDR} (CONFLATE=1)")

    try:
        while True:
            parts = await sock.recv_multipart()
            if len(parts) != 2:
                continue
            meta_raw, frame_raw = parts
            meta = json.loads(meta_raw.decode("utf-8"))
            frame = np.frombuffer(frame_raw, dtype=np.dtype(meta["dtype"])).reshape(tuple(meta["shape"]))
            await hub.set(frame)
    except asyncio.CancelledError:
        pass
    finally:
        sock.close(0)


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse("./index.html")


async def offer(request: web.Request) -> web.Response:
    app = request.app
    params = await request.json()
    remote_offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection(
        configuration=RTCConfiguration(iceServers=[RTCIceServer(urls=["stun:stun.l.google.com:19302"])])
    )
    app["pcs"].add(pc)

    @pc.on("connectionstatechange")
    async def on_connectionstatechange() -> None:
        print(f"[WebRTC] state={pc.connectionState}")
        if pc.connectionState in {"failed", "closed", "disconnected"}:
            await pc.close()
            app["pcs"].discard(pc)

    pc.addTrack(ZMQVideoTrack(app["hub"]))

    await pc.setRemoteDescription(remote_offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})


async def on_startup(app: web.Application) -> None:
    app["hub"] = LatestFrameHub()
    app["pcs"] = set()
    app["sub_task"] = asyncio.create_task(zmq_subscriber(app["hub"]))


async def on_cleanup(app: web.Application) -> None:
    app["sub_task"].cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await app["sub_task"]
    for pc in list(app["pcs"]):
        await pc.close()
    app["pcs"].clear()


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/offer", offer)

    cors = aiohttp_cors.setup(
        app,
        defaults={
            "*": aiohttp_cors.ResourceOptions(
                allow_credentials=True,
                expose_headers="*",
                allow_headers="*",
                allow_methods=["GET", "POST", "OPTIONS"],
            )
        },
    )
    for route in list(app.router.routes()):
        cors.add(route)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    print("[HTTP] Serving on http://0.0.0.0:8080")
    web.run_app(create_app(), host="0.0.0.0", port=8080)
