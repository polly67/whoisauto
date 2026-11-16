(() => {
  let peer = null;
  let pointer = null;
  let keys = null;
  let session = null;
  let timer = null;
  let statsTime = 0;
  let statsBytes = 0;
  let busy = false;
  const video = document.getElementById("desktop-video");
  const status = document.getElementById("desktop-status");
  function sunshine(config) {
    if (config.mode !== "sunshine") return false;
    document.getElementById("webrtc-controls").hidden = true;
    document.getElementById("sunshine-card").hidden = false;
    document.getElementById("sunshine-host").textContent = config.tailscale_ip;
    document.getElementById("sunshine-link").href = config.sunshine_url;
    return true;
  }
  const send = (channel, event) => {
    if (channel?.readyState === "open" && (channel !== pointer || channel.bufferedAmount < 16384)) channel.send(JSON.stringify(event));
  };
  const release = () => send(keys, { type: "release" });
  function position(event) {
    const rect = video.getBoundingClientRect();
    const ratio = video.videoWidth / video.videoHeight || 16 / 9;
    const width = Math.min(rect.width, rect.height * ratio);
    const height = width / ratio;
    const left = rect.left + (rect.width - width) / 2;
    const top = rect.top + (rect.height - height) / 2;
    return { x: Math.max(0, Math.min(1, (event.clientX - left) / width)),
      y: Math.max(0, Math.min(1, (event.clientY - top) / height)) };
  }
  async function stop() {
    release();
    clearInterval(timer);
    timer = null;
    const old = session;
    session = null;
    peer?.close();
    peer = pointer = keys = null;
    video.srcObject = null;
    if (old) await api("/api/desktop/stop", { method: "POST", body: JSON.stringify({ session: old }) }).catch(() => {});
    document.getElementById("desktop-connect").disabled = false;
    status.textContent = "Disconnected.";
  }
  async function connect() {
    if (busy || peer) return;
    busy = true;
    document.getElementById("desktop-connect").disabled = true;
    status.textContent = "Connecting…";
    try {
      const config = await api("/api/desktop/config");
      if (sunshine(config)) return;
      peer = new RTCPeerConnection({ iceServers: config.iceServers, bundlePolicy: "max-bundle" });
      const current = peer;
      peer.ondatachannel = ({ channel }) => {
        if (channel.label === "pointer") pointer = channel;
        else if (channel.label === "keys") keys = channel;
      };
      peer.ontrack = ({ track, streams }) => {
        video.srcObject = streams[0] || new MediaStream([track]);
        video.play().catch(() => { status.textContent = "Tap the video to start playback."; });
      };
      peer.onconnectionstatechange = () => {
        if (current !== peer) return;
        status.textContent = current.connectionState;
        if (["failed", "closed"].includes(current.connectionState)) stop();
      };
      const offer = await api("/api/desktop/offer", { method: "POST",
        body: JSON.stringify({ type: "start" }), timeout: 90000 });
      session = offer.session;
      await peer.setRemoteDescription({ type: "offer", sdp: offer.sdp });
      await peer.setLocalDescription(await peer.createAnswer());
      if (peer.iceGatheringState !== "complete") {
        await new Promise((resolve) => {
          const timeout = setTimeout(resolve, 12000);
          const changed = () => {
            if (current.iceGatheringState === "complete") {
              clearTimeout(timeout);
              current.removeEventListener("icegatheringstatechange", changed);
              resolve();
            }
          };
          current.addEventListener("icegatheringstatechange", changed);
        });
      }
      await api("/api/desktop/answer", { method: "POST",
        body: JSON.stringify({ session, type: "answer", sdp: peer.localDescription.sdp }) });
      statsTime = statsBytes = 0;
      timer = setInterval(async () => {
        if (current !== peer) return;
        try {
          const remote = await api(`/api/desktop/status?session=${encodeURIComponent(session)}`);
          if (remote.error) throw new Error(remote.error);
          const stats = await current.getStats();
          let transport = "Tailscale";
          let rtt = "—";
          let fps = "—";
          let bitrate = "—";
          for (const report of stats.values()) {
            if (report.type === "candidate-pair" && report.state === "succeeded" && report.nominated) {
              transport = "Tailscale media";
              rtt = Math.round((report.currentRoundTripTime || 0) * 1000) + " ms";
            }
            if (report.type === "inbound-rtp" && report.kind === "video") {
              fps = report.framesPerSecond ?? "—";
              if (statsTime) bitrate = ((report.bytesReceived - statsBytes) * 8 / (report.timestamp - statsTime) / 1000).toFixed(1) + " Mbps";
              statsTime = report.timestamp;
              statsBytes = report.bytesReceived;
            }
          }
          status.textContent = `${current.connectionState} · ${transport} · RTT ${rtt} · ${fps} fps · ${bitrate}`;
        } catch (error) { status.textContent = error.message; }
      }, 2000);
    } catch (error) {
      await stop();
      status.textContent = error.message;
    } finally { busy = false; }
  }
  video.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    video.focus();
    video.setPointerCapture(event.pointerId);
    video.play().catch(() => {});
    send(keys, { type: "button", ...position(event), button: event.button || 0, down: true });
  });
  video.addEventListener("pointermove", (event) => send(pointer, { type: "move", ...position(event) }));
  video.addEventListener("pointerup", (event) => send(keys, { type: "button", ...position(event), button: event.button || 0, down: false }));
  video.addEventListener("pointercancel", release);
  video.addEventListener("contextmenu", (event) => event.preventDefault());
  video.addEventListener("wheel", (event) => { event.preventDefault(); send(pointer, { type: "scroll", dy: Math.max(-1000, Math.min(1000, event.deltaY)) }); }, { passive: false });
  for (const type of ["keydown", "keyup"]) video.addEventListener(type, (event) => {
    event.preventDefault();
    send(keys, { type: "key", code: event.code, down: type === "keydown" });
  });
  document.querySelectorAll("[data-remote-key]").forEach((button) => {
    button.addEventListener("pointerdown", (event) => { event.preventDefault(); button.setPointerCapture(event.pointerId); send(keys, { type: "key", code: button.dataset.remoteKey, down: true }); });
    for (const type of ["pointerup", "pointercancel"]) button.addEventListener(type, () => send(keys, { type: "key", code: button.dataset.remoteKey, down: false }));
  });
  document.getElementById("desktop-text").addEventListener("input", (event) => {
    if (event.target.value) send(keys, { type: "text", text: event.target.value });
    event.target.value = "";
  });
  document.getElementById("desktop-text").addEventListener("beforeinput", (event) => {
    if (event.inputType === "deleteContentBackward") {
      send(keys, { type: "key", code: "Backspace", down: true });
      send(keys, { type: "key", code: "Backspace", down: false });
    }
  });
  document.getElementById("desktop-text").addEventListener("keydown", (event) => {
    if (event.code === "Enter") {
      send(keys, { type: "key", code: "Enter", down: true });
      send(keys, { type: "key", code: "Enter", down: false });
    }
  });
  document.getElementById("desktop-connect").addEventListener("click", connect);
  document.getElementById("desktop-disconnect").addEventListener("click", stop);
  document.getElementById("desktop-fullscreen").addEventListener("click", () => video.requestFullscreen?.() || video.webkitEnterFullscreen?.());
  window.addEventListener("blur", release);
  window.addEventListener("pagehide", () => { release(); peer?.close(); });
  api("/api/desktop/config").then(sunshine).catch(() => {});
})();
