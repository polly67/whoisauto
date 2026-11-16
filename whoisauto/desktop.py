"""WebRTC desktop media, signaling, and bounded native input delivery."""

from __future__ import annotations

import json
import ctypes
import ctypes.util
import ipaddress
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from .config import Paths


class DesktopError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code = status, code
        super().__init__(message)


def tailscale_ip() -> str | None:
    value = os.environ.get("WHOISAUTO_TAILSCALE_IP", "")
    if not value:
        return None
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise DesktopError(503, "invalid_tailnet", "Configure a valid Tailscale IPv4 address") from None
    if address not in ipaddress.IPv4Network("100.64.0.0/10"):
        raise DesktopError(503, "invalid_tailnet", "Desktop media must use a Tailscale address")
    return str(address)


def bind_tailnet(peer, address: str) -> None:
    try:
        library_name = ctypes.util.find_library("nice")
        if not library_name:
            for prefix in ("/opt/homebrew", "/usr/local"):
                candidate = Path(prefix) / "opt/libnice/lib/libnice.10.dylib"
                if candidate.is_file():
                    library_name = str(candidate)
                    break
        if not library_name:
            raise ValueError("libnice not found")
        library = ctypes.CDLL(library_name)
        pointer = ctypes.pythonapi.PyCapsule_GetPointer
        pointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
        pointer.restype = ctypes.c_void_p
        gobject_name = ctypes.util.find_library("gobject-2.0")
        if not gobject_name:
            raise ValueError("GObject not found")
        gobject = ctypes.CDLL(gobject_name)
        gobject.g_object_get.restype = None
        gobject.g_object_unref.argtypes = [ctypes.c_void_p]
        peer_pointer = pointer(peer.__gpointer__, None)
        ice_pointer, agent_pointer = ctypes.c_void_p(), ctypes.c_void_p()
        gobject.g_object_get(ctypes.c_void_p(peer_pointer), ctypes.c_char_p(b"ice-agent"), ctypes.byref(ice_pointer), None)
        if not ice_pointer.value:
            raise ValueError("ICE agent unavailable")
        gobject.g_object_get(ice_pointer, ctypes.c_char_p(b"agent"), ctypes.byref(agent_pointer), None)
        if not agent_pointer.value:
            gobject.g_object_unref(ice_pointer)
            raise ValueError("native agent unavailable")
        library.nice_address_new.restype = ctypes.c_void_p
        library.nice_address_set_from_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        library.nice_address_set_from_string.restype = ctypes.c_int
        library.nice_agent_add_local_address.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        library.nice_agent_add_local_address.restype = ctypes.c_int
        library.nice_address_free.argtypes = [ctypes.c_void_p]
        local = library.nice_address_new()
        try:
            accepted = library.nice_address_set_from_string(local, address.encode("ascii"))
            if accepted:
                accepted = library.nice_agent_add_local_address(agent_pointer, local)
        finally:
            library.nice_address_free(local)
            gobject.g_object_unref(agent_pointer)
            gobject.g_object_unref(ice_pointer)
        if not accepted:
            raise ValueError("local address rejected")
    except (OSError, ValueError, AttributeError):
        raise DesktopError(503, "tailnet_unavailable", "Could not select the Tailscale media interface") from None


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
        address = tailscale_ip()
        return {"ok": True, "iceServers": [], "transport": "tailscale",
                "tailscale_ip": address,
                "width": 1280, "height": 720, "fps": 60, "bitrate": 6000000}

    def offer(self, body: dict, *, test: bool = False) -> dict:
        if body.get("type") != "start":
            raise DesktopError(400, "invalid_offer", "Start a host-generated WebRTC session")
        payload = 96
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
                address = tailscale_ip()
                if address and not test:
                    bind_tailnet(self.peer, address)
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
                done = threading.Event()
                answer = {}
                for label in ("pointer", "keys"):
                    options = self.Gst.Structure.new_empty("application/data-channel")
                    options.set_value("ordered", label != "pointer")
                    if label == "pointer": options.set_value("max-retransmits", 0)
                    channel = self.peer.emit("create-data-channel", label, options)
                    if not channel: raise DesktopError(503, "negotiation_failed", "Could not create input channel")
                    self._channel(self.peer, channel)

                def local_offer_created(promise, *args):
                    try:
                        reply = promise.get_reply()
                        value = reply.get_value("offer")
                        def local_set(result, *args):
                            answer["value"] = value
                            done.set()
                        local = Gst.Promise.new_with_change_func(local_set, None, None)
                        self.peer.emit("set-local-description", value, local)
                    except Exception:
                        answer["error"] = True
                        done.set()
                promise = Gst.Promise.new_with_change_func(local_offer_created, None, None)
                self.peer.emit("create-offer", None, promise)
                if not done.wait(10) or answer.get("error"):
                    raise DesktopError(503, "negotiation_failed", "Could not create a WebRTC offer")
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
                return {"ok": True, "session": self.session, "type": "offer", "sdp": local.sdp.as_text()}
            except DesktopError:
                self.close()
                raise
            except Exception:
                self.close()
                raise DesktopError(503, "webrtc_failed", "Could not start capture or negotiate H.264 WebRTC") from None

    def answer(self, body: dict) -> dict:
        sdp = body.get("sdp")
        if body.get("type") != "answer" or not isinstance(sdp, str) or len(sdp) > 128 * 1024:
            raise DesktopError(400, "invalid_answer", "Send a WebRTC answer")
        with self.lock:
            if body.get("session") != self.session or not self.peer:
                raise DesktopError(404, "session_not_found", "Desktop session ended")
            _, message = self.Sdp.SDPMessage.new()
            if self.Sdp.sdp_message_parse_buffer(sdp.encode(), message) != self.Sdp.SDPResult.OK:
                raise DesktopError(400, "invalid_answer", "Could not parse SDP")
            description = self.RTC.WebRTCSessionDescription.new(self.RTC.WebRTCSDPType.ANSWER, message)
            promise = self.Gst.Promise.new()
            self.peer.emit("set-remote-description", description, promise)
            if promise.wait() != self.Gst.PromiseResult.REPLIED:
                raise DesktopError(503, "negotiation_failed", "WebRTC answer was rejected")
            self._last_seen = time.monotonic()
            return {"ok": True}

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

