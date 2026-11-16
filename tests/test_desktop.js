const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require("node:path").join(__dirname, "../whoisauto/static/desktop.js"), "utf8");
const tick = () => new Promise(setImmediate);

function setup(config = { iceServers: [] }) {
  const ids = ["desktop-video", "desktop-status", "desktop-connect", "desktop-disconnect", "desktop-fullscreen", "desktop-text", "webrtc-controls", "sunshine-card", "sunshine-host", "sunshine-link"];
  const elements = new Map(ids.map((id) => [id, {
    listeners: {}, value: "", disabled: false, videoWidth: 1280, videoHeight: 720,
    addEventListener(name, handler) { this.listeners[name] = handler; },
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 1280, height: 720 }),
    focus() {}, setPointerCapture() {}, play: async () => {},
  }]));
  const calls = [];
  const channels = [];
  const peers = [];
  class Peer {
    constructor() { this.iceGatheringState = "complete"; peers.push(this); }
    addTransceiver() { return { setCodecPreferences() {} }; }
    createDataChannel(label, options) {
      const channel = { label, options, readyState: "open", bufferedAmount: 0, events: [],
        send(text) { this.events.push(JSON.parse(text)); } };
      channels.push(channel);
      return channel;
    }
    async createAnswer() { return { type: "answer", sdp: "test answer" }; }
    async setLocalDescription(value) { this.localDescription = value; }
    async setRemoteDescription(value) {
      this.remoteDescription = value;
      for (const label of ["pointer", "keys"]) {
        const channel = this.createDataChannel(label, { ordered: label !== "pointer", ...(label === "pointer" ? { maxRetransmits: 0 } : {}) });
        this.ondatachannel?.({ channel });
      }
    }
    close() { this.closed = true; }
  }
  const context = vm.createContext({
    document: { getElementById: (id) => elements.get(id), querySelectorAll: () => [] },
    window: { addEventListener() {} }, RTCPeerConnection: Peer,
    RTCRtpReceiver: { getCapabilities: () => ({ codecs: [{ mimeType: "video/H264" }] }) },
    api: async (url, options) => {
      calls.push({ url, options });
      if (url.endsWith("config")) return config;
      if (url.endsWith("offer")) return { session: "session-1", type: "offer", sdp: "offer" };
      return { ok: true };
    }, setInterval: () => 0, clearInterval() {}, setTimeout, clearTimeout,
  });
  vm.runInContext(source, context);
  return { elements, channels, calls, peers };
}

test("browser requests receive-only WebRTC and separates pointer delivery", async () => {
  const { elements, channels, calls } = setup();
  await elements.get("desktop-connect").listeners.click();
  assert.equal(channels[0].label, "pointer");
  assert.equal(channels[0].options.ordered, false);
  assert.equal(channels[0].options.maxRetransmits, 0);
  assert.equal(channels[1].label, "keys");
  assert.equal(channels[1].options.ordered, true);
  assert.ok(calls.some(call => call.url === "/api/desktop/offer"));
});

test("mobile touches send normalized coordinates and reliable button releases", async () => {
  const { elements, channels } = setup();
  await elements.get("desktop-connect").listeners.click();
  const event = { clientX: 640, clientY: 360, button: 0, pointerId: 1, preventDefault() {} };
  elements.get("desktop-video").listeners.pointerdown(event);
  elements.get("desktop-video").listeners.pointerup(event);
  assert.equal(channels[1].events[0].x, 0.5);
  assert.equal(channels[1].events[0].y, 0.5);
  assert.equal(channels[1].events[1].down, false);
});

test("mobile keyboard deletion works and disconnect releases held input", async () => {
  const { elements, channels, calls, peers } = setup();
  await elements.get("desktop-connect").listeners.click();
  elements.get("desktop-text").listeners.beforeinput({ inputType: "deleteContentBackward" });
  assert.equal(channels[1].events[0].code, "Backspace");
  assert.equal(channels[1].events[1].down, false);
  await elements.get("desktop-disconnect").listeners.click();
  assert.equal(channels[1].events.at(-1).type, "release");
  assert.equal(peers[0].closed, true);
  assert.equal(calls.at(-1).url, "/api/desktop/stop");
});

test("Sunshine mode shows Moonlight details without starting WebRTC", async () => {
  const { elements, calls, peers } = setup({
    mode: "sunshine", tailscale_ip: "100.85.55.26", sunshine_url: "https://100.85.55.26:47990",
  });
  await tick();
  assert.equal(elements.get("webrtc-controls").hidden, true);
  assert.equal(elements.get("sunshine-card").hidden, false);
  assert.equal(elements.get("sunshine-host").textContent, "100.85.55.26");
  assert.equal(elements.get("sunshine-link").href, "https://100.85.55.26:47990");
  await elements.get("desktop-connect").listeners.click();
  assert.equal(peers.length, 0);
  assert.ok(calls.every(call => call.url === "/api/desktop/config"));
});
