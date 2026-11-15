"""WebRTC desktop media, signaling, and bounded native input delivery."""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

from .config import Paths


class DesktopError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code = status, code
        super().__init__(message)


def ice_servers() -> list[dict]:
    servers = [{"urls": "stun:stun.l.google.com:19302"}]
    url = os.environ.get("WHOISAUTO_TURN_URL", "")
    user = os.environ.get("WHOISAUTO_TURN_USERNAME", "")
    credential = os.environ.get("WHOISAUTO_TURN_CREDENTIAL", "")
    if any((url, user, credential)):
        if not all((url, user, credential)) or not url.startswith(("turn:", "turns:")):
            raise DesktopError(503, "invalid_turn", "Configure the TURN URL, username, and credential together")
        servers.append({"urls": url, "username": user, "credential": credential})
    return servers


def turn_uri(server: dict) -> str:
    scheme, address = server["urls"].split(":", 1)
    user = urllib.parse.quote(server["username"], safe="")
    credential = urllib.parse.quote(server["credential"], safe="")
    return f"{scheme}://{user}:{credential}@{address}"


def input_event(raw: str) -> dict | None:
    if len(raw) > 1024:
        return None
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(event, dict):
        return None
    kind = event.get("type")
    if kind in ("move", "button"):
        if any(type(event.get(key)) not in (int, float) or not math.isfinite(event[key])
               or not 0 <= event[key] <= 1 for key in ("x", "y")):
            return None
        if kind == "button" and (type(event.get("button")) is not int or event["button"] not in (0, 1, 2) or type(event.get("down")) is not bool):
            return None
    elif kind == "key":
        if not isinstance(event.get("code"), str) or len(event["code"]) > 32 or type(event.get("down")) is not bool:
            return None
    elif kind == "text":
        if not isinstance(event.get("text"), str) or len(event["text"]) > 256:
            return None
    elif kind == "scroll":
        if type(event.get("dy")) not in (int, float) or not math.isfinite(event["dy"]) or abs(event["dy"]) > 1000:
            return None
    elif kind != "release":
        return None
    return event


def h264_payload(sdp: str) -> int:
    compatible = []
    for value in re.finditer(r"^a=rtpmap:(\d+) H264/90000\s*$", sdp, re.MULTILINE | re.IGNORECASE):
        payload = value.group(1)
        fmtp = re.search(r"^a=fmtp:" + payload + r" (.+)$", sdp, re.MULTILINE)
        parameters = fmtp.group(1).lower() if fmtp else ""
        profile = re.search(r"profile-level-id=([0-9a-f]+)", parameters)
        if "packetization-mode=1" in parameters and (not profile or profile.group(1).startswith("42")):
            compatible.append((0 if profile and profile.group(1).startswith("42e") else 1, int(payload)))
    if compatible:
        return min(compatible)[1]
    raise DesktopError(400, "unsupported_codec", "The browser must offer baseline H.264 with packetization mode 1")


class NativeInput:
    def __init__(self, paths: Paths):
        self.paths = paths
        self.child = None
        self.lock = threading.Lock()

    def start(self):
        if sys.platform != "darwin":
            raise DesktopError(400, "unsupported_platform", "Desktop control requires macOS")
        source = Path(__file__).parent / "native" / "desktop_input.swift"
        binary = self.paths.state / "desktop-input"
        self.paths.state.mkdir(parents=True, exist_ok=True)
        try:
            if not binary.is_file() or binary.stat().st_mtime < source.stat().st_mtime:
                result = subprocess.run(["xcrun", "swiftc", str(source), "-O", "-o", str(binary)],
                                        capture_output=True, timeout=60)
                if result.returncode:
                    raise DesktopError(503, "input_unavailable", "Could not build desktop input helper")
            self.child = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL, text=True, bufsize=1)
            ready = self.child.stdout.readline().strip()
            if ready != "READY":
                self.close()
                raise DesktopError(503, "accessibility_required", "Allow desktop input in macOS Accessibility settings")
        except OSError:
            self.close()
            raise DesktopError(503, "input_unavailable", "Could not start desktop input helper") from None

    def send(self, raw: str):
        event = input_event(raw)
        if event is None or not self.child:
            return
        with self.lock:
            try:
                self.child.stdin.write(json.dumps(event) + "\n")
                self.child.stdin.flush()
            except (OSError, BrokenPipeError):
                pass

    def close(self):
        if self.child:
            try:
                self.child.stdin.write('{"type":"release"}\n')
                self.child.stdin.close()
                self.child.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                self.child.kill()
                self.child.wait()
            self.child = None


class WebRTCDesktop:
    def __init__(self, paths: Paths):
        self.paths = paths
        self.lock = threading.RLock()
        self.pipe = self.peer = self.session = self.input = None
        self._last_seen = 0
        self._error = None
        self._glib = None

    def _runtime(self):
        if self._glib:
            return
        try:
            import gi
            gi.require_version("Gst", "1.0")
            gi.require_version("GstWebRTC", "1.0")
            gi.require_version("GstSdp", "1.0")
            from gi.repository import Gst, GstWebRTC, GstSdp, GLib
        except (ImportError, ValueError):
            raise DesktopError(503, "webrtc_unavailable", "Install GStreamer with WebRTC and PyGObject support") from None
        self.Gst, self.RTC, self.Sdp, self.GLib = Gst, GstWebRTC, GstSdp, GLib
        Gst.init(None)
        self._glib = GLib.MainLoop()
        threading.Thread(target=self._glib.run, daemon=True).start()
        GLib.timeout_add_seconds(5, self._expire)

    def _expire(self):
        if not self.lock.acquire(blocking=False):
            return True
        try:
            if self.session and time.monotonic() - self._last_seen > 20:
                self.close()
        finally:
            self.lock.release()
        return True

    def config(self) -> dict:
        servers = ice_servers()
        return {"ok": True, "iceServers": servers, "relay_available": len(servers) > 1,
                "width": 1280, "height": 720, "fps": 60, "bitrate": 6000000}

    def offer(self, body: dict, *, test: bool = False) -> dict:
        sdp = body.get("sdp")
        if body.get("type") != "offer" or not isinstance(sdp, str) or len(sdp) > 128 * 1024 or "v=0" not in sdp:
            raise DesktopError(400, "invalid_offer", "Send a valid WebRTC SDP offer")
        payload = h264_payload(sdp)
        with self.lock:
            if self.session and time.monotonic() - self._last_seen < 20:
                raise DesktopError(409, "desktop_busy", "Another desktop session is connected")
            self.close()
            self._runtime()
            Gst = self.Gst
            if not test and sys.platform != "darwin":
                raise DesktopError(400, "unsupported_platform", "Desktop streaming requires macOS")
            encoder = "x264enc tune=zerolatency speed-preset=ultrafast" if test else "vtenc_h264 realtime=true allow-frame-reordering=false"
            source = "videotestsrc is-live=true pattern=ball" if test else "avfvideosrc capture-screen=true capture-screen-cursor=true"
            keyframe = "key-int-max=60" if test else "max-keyframe-interval=60"
            pipeline = (f"{source} ! video/x-raw,framerate=60/1 ! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream "
                        f"! videoconvert ! videoscale ! video/x-raw,width=1280,height=720 "
                        f"! {encoder} name=encoder bitrate=6000 {keyframe} ")
            pipeline += ("! h264parse config-interval=-1 ! video/x-h264,profile=constrained-baseline "
                         f"! rtph264pay name=pay pt={payload} config-interval=-1 aggregate-mode=zero-latency "
                         f"! application/x-rtp,media=video,encoding-name=H264,payload={payload},clock-rate=90000 "
                         "! webrtcbin name=peer bundle-policy=max-bundle")
            try:
                self.pipe = Gst.parse_launch(pipeline)
                self.peer = self.pipe.get_by_name("peer")
                self.peer.set_property("stun-server", "stun://stun.l.google.com:19302")
                for server in ice_servers()[1:]:
                    if not self.peer.emit("add-turn-server", turn_uri(server)):
                        raise DesktopError(503, "invalid_turn", "The TURN server URL could not be configured")
                self.peer.connect("on-data-channel", self._channel)
                self.pipe.get_bus().add_signal_watch()
                self.pipe.get_bus().connect("message::error", self._bus_error)
                if not test:
                    self.input = NativeInput(self.paths)
                    self.input.start()
                self._error = None
                self.session = uuid.uuid4().hex
                self._last_seen = time.monotonic()
                self.pipe.set_state(Gst.State.PLAYING)
                deadline = time.monotonic() + 5
                pay = self.pipe.get_by_name("pay")
                while not pay.get_static_pad("src").get_current_caps() and time.monotonic() < deadline:
                    if self._error:
                        raise DesktopError(503, "capture_failed", self._error)
                    time.sleep(0.02)
                _, message = self.Sdp.SDPMessage.new()
                if self.Sdp.sdp_message_parse_buffer(sdp.encode(), message) != self.Sdp.SDPResult.OK:
                    raise DesktopError(400, "invalid_offer", "Could not parse SDP")
                description = self.RTC.WebRTCSessionDescription.new(self.RTC.WebRTCSDPType.OFFER, message)
                done = threading.Event()
                answer = {}

                def created(promise, *args):
                    try:
                        reply = promise.get_reply()
                        value = reply.get_value("answer")
                        text = value.sdp.as_text()
                        if "a=setup:actpass" in text:
                            _, fixed = self.Sdp.SDPMessage.new()
                            self.Sdp.sdp_message_parse_buffer(text.replace("a=setup:actpass", "a=setup:active").encode(), fixed)
                            value = self.RTC.WebRTCSessionDescription.new(self.RTC.WebRTCSDPType.ANSWER, fixed)
                        def local_set(result, *args):
                            answer["value"] = value
                            done.set()
                        local = Gst.Promise.new_with_change_func(local_set, None, None)
                        self.peer.emit("set-local-description", value, local)
                    except Exception:
                        answer["error"] = True
                        done.set()

                def remote_set(promise, *args):
                    result = Gst.Promise.new_with_change_func(created, None, None)
                    self.peer.emit("create-answer", None, result)

                promise = Gst.Promise.new_with_change_func(remote_set, None, None)
                self.peer.emit("set-remote-description", description, promise)
                if not done.wait(10) or answer.get("error"):
                    raise DesktopError(503, "negotiation_failed", "Could not create a WebRTC answer")
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    if self._error:
                        raise DesktopError(503, "capture_failed", self._error)
                    if self.peer.get_property("ice-gathering-state") == self.RTC.WebRTCICEGatheringState.COMPLETE:
                        break
                    time.sleep(0.05)
                local = self.peer.get_property("local-description")
                if not local:
                    raise DesktopError(503, "negotiation_failed", "The WebRTC answer is unavailable")
                return {"ok": True, "session": self.session, "type": "answer", "sdp": local.sdp.as_text()}
            except DesktopError:
                self.close()
                raise
            except Exception:
                self.close()
                raise DesktopError(503, "webrtc_failed", "Could not start capture or negotiate H.264 WebRTC") from None

    def _bus_error(self, bus, message):
        self._error = "Screen capture or encoding failed; check macOS Screen Recording permission"

    def _channel(self, peer, channel):
        if channel.get_property("label") not in ("pointer", "keys"):
            channel.close()
            return
        channel.connect("on-message-string", lambda ch, raw: self.input.send(raw) if self.input else None)
        channel.connect("on-close", lambda ch: self.input.send('{"type":"release"}') if self.input else None)

    def status(self, session: str) -> dict:
        with self.lock:
            if session != self.session or not self.peer:
                raise DesktopError(404, "session_not_found", "Desktop session ended")
            self._last_seen = time.monotonic()
            return {"ok": True, "connection": self.peer.get_property("connection-state").value_nick,
                    "error": self._error}

    def stop(self, session: str) -> dict:
        with self.lock:
            if session == self.session:
                self.close()
            return {"ok": True}

    def close(self):
        if self.pipe:
            self.pipe.set_state(self.Gst.State.NULL)
            self.pipe.get_bus().remove_signal_watch()
        if self.input:
            self.input.close()
        self.pipe = self.peer = self.session = self.input = None

    def shutdown(self):
        with self.lock:
            self.close()
            if self._glib:
                self._glib.quit()
                self._glib = None

