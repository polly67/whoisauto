"""Exercise real H.264 WebRTC with a synthetic source and a Python receiver."""
import asyncio
import json
from pathlib import Path
import tempfile

from aiortc import RTCPeerConnection, RTCSessionDescription, RTCRtpReceiver
from whoisauto.config import Paths
from whoisauto.desktop import WebRTCDesktop


async def run():
    with tempfile.TemporaryDirectory(prefix="whoisauto-webrtc-") as temporary:
        root = Path(temporary)
        paths = Paths(root, root, root / "state", root / "apps", root / "workspace", root / "installers")
        desktop = WebRTCDesktop(paths)
        client = RTCPeerConnection()
        track = asyncio.get_running_loop().create_future()

        @client.on("track")
        def received(value):
            if not track.done(): track.set_result(value)

        transceiver = client.addTransceiver("video", direction="recvonly")
        transceiver.setCodecPreferences([codec for codec in RTCRtpReceiver.getCapabilities("video").codecs
                                        if codec.mimeType.lower() == "video/h264"])
        client.createDataChannel("keys")
        try:
            await client.setLocalDescription(await client.createOffer())
            answer = await asyncio.to_thread(desktop.offer,
                {"type": "offer", "sdp": client.localDescription.sdp}, test=True)
            await client.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type="answer"))
            video = await asyncio.wait_for(track, timeout=15)
            frame = await asyncio.wait_for(video.recv(), timeout=20)
            assert frame.width == 1280 and frame.height == 720
            print(json.dumps({"codec": "H264", "width": frame.width, "height": frame.height,
                              "connection": client.connectionState}))
        finally:
            desktop.close()
            if desktop._glib: desktop._glib.quit()
            await client.close()


asyncio.run(run())
