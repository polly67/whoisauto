"""Exercise real H.264 WebRTC with a synthetic source and a Python receiver."""
import asyncio
import json
from pathlib import Path
import tempfile

from aiortc import RTCPeerConnection, RTCSessionDescription, RTCRtpReceiver, RTCCertificate
from whoisauto.config import Paths
from whoisauto.desktop import WebRTCDesktop


# Match modern browser DTLS cipher support. aiortc's default context only offers
# ECDSA, while GStreamer's default certificate is RSA. Add a strong ECDHE/RSA
# cipher for this test receiver; certificate fingerprint checks remain enabled.
original_context = RTCCertificate._create_ssl_context
def browser_context(self, srtp_profiles):
    context = original_context(self, srtp_profiles)
    context.set_cipher_list(b"ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256")
    return context
RTCCertificate._create_ssl_context = browser_context

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

        try:
            offer = await asyncio.to_thread(desktop.offer, {"type": "start"}, test=True)
            print(json.dumps({"offer_media": [line for line in offer["sdp"].splitlines()
                if line.startswith(("m=", "a=send", "a=recv", "a=inactive", "a=rtpmap", "a=fmtp", "a=setup"))]}))
            await client.setRemoteDescription(RTCSessionDescription(sdp=offer["sdp"], type="offer"))
            await client.setLocalDescription(await client.createAnswer())
            await asyncio.to_thread(desktop.answer, {"session": offer["session"], "type": "answer", "sdp": client.localDescription.sdp})
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
