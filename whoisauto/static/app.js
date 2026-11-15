const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const { timeout = 70000, ...requestOptions } = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" }, ...requestOptions, signal: controller.signal,
    });
    let data;
    try {
      data = await response.json();
      if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("Invalid JSON object");
    } catch {
      throw new Error(`Invalid response from the panel (HTTP ${response.status}).`);
    }
    if (!response.ok) {
      const error = new Error(data.message || response.statusText);
      error.data = data;
      error.status = response.status;
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error("The request timed out. The action may still finish; check its status before retrying.");
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

async function action(ids, target, pending, run, after = () => {}) {
  const buttons = (Array.isArray(ids) ? ids : [ids]).map($);
  if (buttons.some((button) => button.dataset.busy === "true")) return;
  buttons.forEach((button) => { button.disabled = true; button.dataset.busy = "true"; });
  if (pending) log(target, pending);
  try {
    await run();
  } catch (error) {
    log(target, `${error.data?.error || "error"}: ${error.message}`, "err");
  } finally {
    buttons.forEach((button) => { button.disabled = false; delete button.dataset.busy; });
    after();
  }
}

function log(target, message, kind = "") {
  const el = typeof target === "string" ? $(target) : target;
  const stamp = new Date().toLocaleTimeString();
  el.textContent = `[${stamp}] ${message}`;
  el.className = "log" + (kind ? " " + kind : "") + (el.classList.contains("tall") ? " tall" : "");
}

function badge(el, ok, text) {
  el.textContent = text;
  el.className = "badge " + (ok ? "good" : "bad");
}

function pill(el, ok, text) {
  el.textContent = text;
  el.className = "pill " + (ok ? "good" : "bad");
}

function pretty(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/* ------------------------------------------------------------------ tabs */

function activate(tab) {
  document.querySelectorAll(".tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === tab);
    button.setAttribute("aria-selected", String(button.dataset.tab === tab));
    button.tabIndex = button.dataset.tab === tab ? 0 : -1;
  });
  document.querySelectorAll(".panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `tab-${tab}`);
  });
  if (tab === "players") loadPlayers(true);
}

document.querySelectorAll(".tab").forEach((button) => {
  button.id = `tab-${button.dataset.tab}-button`;
  button.setAttribute("aria-controls", `tab-${button.dataset.tab}`);
  const panel = $(`tab-${button.dataset.tab}`);
  panel.setAttribute("role", "tabpanel");
  panel.setAttribute("aria-labelledby", button.id);
  button.addEventListener("click", () => {
    activate(button.dataset.tab);
    history.replaceState(null, "", `#${button.dataset.tab}`);
  });
  button.addEventListener("keydown", (event) => {
    const tabs = [...document.querySelectorAll(".tab")];
    const index = tabs.indexOf(button);
    const next = event.key === "ArrowRight" ? (index + 1) % tabs.length
      : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length
      : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : null;
    if (next === null) return;
    event.preventDefault();
    tabs[next].focus();
    tabs[next].click();
  });
});

/* ----------------------------------------------------------------- state */

function renderInstall(install) {
  const editor = install.editor || {};
  const roblox = install.roblox || {};
  badge($("editor-badge"), editor.available, editor.available ? "ready" : "missing");
  badge($("roblox-badge"), roblox.available, roblox.available ? "ready" : "missing");
  $("editor-path").textContent = editor.path || "—";
  $("roblox-path").textContent = roblox.path || "—";
  if (editor.problem) $("editor-path").textContent += ` — ${editor.problem}`;
  if (roblox.problem) $("roblox-path").textContent += ` — ${roblox.problem}`;
  const ready = editor.available && roblox.available;
  pill($("pill-install"), ready, `install: ${ready ? "ready" : "incomplete"}`);
}

function renderCompiler(compiler) {
  const value = compiler || {};
  badge($("compiler-badge"), value.available, value.available ? "ready" : "missing");
  $("compiler-path").textContent = value.available
    ? value.path
    : `${value.path || "—"} — Execute refuses scripts until this is built`;
}

function renderService(service) {
  const running = service.running;
  $("dot-api").className = "dot " + (running ? "on" : "off");
  badge($("api-badge"), running, running ? `port ${service.port}` : "stopped");
  $("api-path").textContent = running
    ? `http://127.0.0.1:${service.port}  ·  pid ${service.health?.pid ?? "?"}  ·  log ${service.log}`
    : `preferred port ${service.preferred}${service.preferred_free ? " (free)" : " (busy)"}  ·  log ${service.log}`;
  pill($("pill-api"), running, running ? `api: :${service.port}` : "api: down");
}

function renderAccount(account) {
  const card = $("account-card");
  card.replaceChildren();
  if (!account.signed_in) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = "No account saved.";
    card.appendChild(p);
    pill($("pill-account"), false, "account: none");
    return;
  }
  const user = account.user || {};
  const title = document.createElement("h3");
  title.textContent = user.display_name || user.name || "account";
  const line = document.createElement("p");
  line.className = "path";
  line.textContent = `@${user.name}  ·  user id ${user.id}  ·  token ${account.masked}`;
  card.append(title, line);
  pill($("pill-account"), true, `account: ${user.name}`);
}

let stateBusy = false;
async function refreshState() {
  if (stateBusy) return;
  stateBusy = true;
  try {
    const state = await api("/api/state");
    renderInstall(state.install || {});
    renderCompiler(state.compiler || {});
    if (state.autoexec) renderAutoExec(state.autoexec);
    renderService(state.service || {});
    renderAccount(state.account || { signed_in: false });
  } catch (error) {
    pill($("pill-api"), false, "api: panel unreachable");
    $("dot-api").className = "dot off";
    console.error(error);
  } finally {
    stateBusy = false;
  }
}

/* ------------------------------------------------------------------ setup */

$("install-btn").addEventListener("click", () => action("install-btn", "setup-log",
  "Installing Opiumware and the patched Roblox app into the opom root…", async () => {
    const result = await api("/api/install", {
      method: "POST",
      timeout: 1800000,
      body: JSON.stringify({
        editor_source: $("editor-source").value.trim(),
        roblox_source: $("roblox-source").value.trim(),
        launch: $("launch-after").checked,
      }),
    });
    log("setup-log", pretty(result), result.ok ? "ok" : "err");
  }, refreshState));

async function serviceAction(path, label) {
  return action(["start-api", "stop-api"], "setup-log", `${label}…`, async () => {
    const result = await api(path, { method: "POST", body: "{}" });
    log("setup-log", pretty(result), "ok");
  }, refreshState);
}

$("build-compiler").addEventListener("click", () => action("build-compiler", "setup-log",
  "Building the Luau syntax checker (clones Luau and compiles it; a few minutes)…", async () => {
    const result = await api("/api/compiler/build", { method: "POST", body: "{}", timeout: 1230000 });
    log("setup-log", `built ${result.path} (${result.bytes} bytes)\n${result.log || ""}`, "ok");
  }, refreshState));

$("start-api").addEventListener("click", () => serviceAction("/api/service/start", "Starting opom"));
$("stop-api").addEventListener("click", () => serviceAction("/api/service/stop", "Stopping opom"));

/* ---------------------------------------------------------------- account */

const accountControls = ["save-token", "clear-token", "open-roblox", "restart-roblox", "stop-roblox"];

function playerOpened(result) {
  const port = result.instance?.port;
  return port ? `Roblox opened on port ${port}.` : "Roblox opened; its execution port is not ready yet.";
}

$("save-token").addEventListener("click", () => {
  const token = $("token").value.trim();
  if (!token) {
    log("account-log", "Paste a token first.", "err");
    return;
  }
  return action(accountControls, "account-log", "Verifying account and opening Roblox…", async () => {
    let result;
    try {
      result = await api("/api/account", {
        method: "POST", body: JSON.stringify({ token }), timeout: 240000,
      });
    } catch (error) {
      if (error.data?.account) {
        $("token").value = "";
        renderAccount(error.data.account);
      }
      throw error;
    }
    $("token").value = "";
    log("account-log", `Saved ${result.account.user.name} (${result.account.user.id}).` +
      (result.player ? ` ${playerOpened(result.player)}` : ""), "ok");
    renderAccount(result.account);
  }, refreshState);
});

$("clear-token").addEventListener("click", () => action(accountControls,
  "account-log", "Clearing saved account…", async () => {
    const result = await api("/api/account", { method: "DELETE" });
    renderAccount(result.account);
    log("account-log", "Token removed from local state.", "ok");
  }));

function playerAction(operation, pending) {
  return action(accountControls, "account-log", pending, async () => {
    const result = await api(`/api/roblox/${operation}`, { method: "POST", body: "{}", timeout: 240000 });
    log("account-log", operation === "stop"
      ? `Stopped ${result.stopped.length} Roblox Player instance(s).`
      : result.reused ? `Roblox is already running on port ${result.instance.port}.`
      : playerOpened(result), "ok");
  }, refreshState);
}
$("open-roblox").addEventListener("click", () => playerAction("open", "Opening Roblox…"));
$("restart-roblox").addEventListener("click", () => playerAction("restart", "Restarting Roblox with the saved account…"));
$("stop-roblox").addEventListener("click", () => playerAction("stop", "Stopping Roblox…"));

/* ------------------------------------------------------------------- join */

async function previewPlace() {
  const placeId = $("place-id").value.trim();
  if (!placeId) {
    log("join-log", "Enter a place id first.", "err");
    return;
  }
  const card = $("place-card");
  card.replaceChildren(Object.assign(document.createElement("p"),
    { className: "muted", textContent: "Looking up place…" }));
  try {
    const data = await api(`/api/players?place_id=${encodeURIComponent(placeId)}`);
    renderPlace(data.place, card);
    if (data.place_error) {
      log("join-log", `${data.place_error.error}: ${data.place_error.message}`, "err");
    }
  } catch (error) {
    log("join-log", error.message, "err");
  }
}

function renderPlace(place, card) {
  card.replaceChildren();
  if (!place) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = "No place details available.";
    card.appendChild(p);
    return;
  }
  const title = document.createElement("h3");
  title.textContent = place.name || `Place ${place.place_id}`;
  const line = document.createElement("p");
  line.className = "path";
  line.textContent = `place ${place.place_id} · universe ${place.universe_id} · ` +
    `${place.playing ?? "?"}/${place.max_players ?? "?"} playing · by ${place.creator || "unknown"}`;
  card.append(title, line);
}

/* ------------------------------------------------------------ auto-execute */

let autoexecLoaded = false;
let autoexecRevision = 0;
["autoexec-script", "autoexec-name", "autoexec-enabled", "autoexec-delay", "autoexec-wait-for"].forEach((id) => {
  $(id).addEventListener("input", () => { autoexecLoaded = true; autoexecRevision++; });
});

function renderAutoExec(snapshot) {
  const config = snapshot.config || {};
  const job = snapshot.job || {};
  const last = snapshot.last || {};
  const name = config.name || "autoexec.luau";

  // /api/state omits the script, so only the full snapshot may fill the form
  if (!autoexecLoaded && typeof config.script === "string") {
    $("autoexec-enabled").checked = Boolean(config.enabled);
    $("autoexec-name").value = config.name || "";
    $("autoexec-delay").value = config.delay ?? 3;
    $("autoexec-wait-for").value = config.wait_for ?? 90;
    $("autoexec-script").value = config.script || "";
    autoexecLoaded = true;
  }

  let text;
  let tone = "";
  if (job.status === "waiting") {
    text = `${job.script} · waiting for place ${job.place_id} (${job.trigger})`;
    tone = "good";
  } else if (job.status === "running") {
    text = `${job.script} · running in place ${job.place_id}`;
    tone = "good";
  } else if (job.status === "done") {
    text = `${job.script} · executed in place ${job.place_id} at ${job.finished_at}`;
    tone = "good";
  } else if (job.status === "cancelled") {
    text = `${job.script} · cancelled before it ran`;
  } else if (job.status === "timeout" || job.status === "failed") {
    text = `${job.script} · ${job.status}: ${job.message || job.error || ""}`;
    tone = "bad";
  } else if (config.enabled && config.bytes) {
    text = `${name} · armed (${config.bytes} bytes), runs on the next join or rejoin`;
    tone = "good";
  } else if (config.enabled) {
    text = `${name} · enabled but empty; nothing will run`;
    tone = "bad";
  } else {
    text = last.script ? `${last.script} · off (last run ${last.status})` : "Off.";
  }

  $("autoexec-status").textContent = text;
  const label = tone === "good" ? "on" : tone === "bad" ? "error" : "off";
  $("autoexec-badge").textContent = label;
  $("autoexec-badge").className = "badge" + (tone ? " " + tone : "");
  $("autoexec-cancel").disabled = job.status !== "waiting" || $("autoexec-cancel").dataset.busy === "true";
}

async function loadAutoExec() {
  try {
    renderAutoExec(await api("/api/autoexec"));
  } catch (error) {
    log("join-log", `auto-execute: ${error.message}`, "err");
  }
}

$("autoexec-save").addEventListener("click", () => action("autoexec-save", "join-log",
  "Saving auto-execute…", async () => {
    const revision = autoexecRevision;
    const result = await api("/api/autoexec", {
      method: "POST",
      body: JSON.stringify({
        enabled: $("autoexec-enabled").checked,
        name: $("autoexec-name").value.trim(),
        delay: Number($("autoexec-delay").value || 0),
        wait_for: Number($("autoexec-wait-for").value || 90),
        script: $("autoexec-script").value,
      }),
    });
    if (revision === autoexecRevision) autoexecLoaded = false;
    renderAutoExec(result);
    log("join-log", result.config.enabled
      ? `Auto-execute armed: ${result.config.name} (${result.config.bytes} bytes).`
      : "Auto-execute disabled.", "ok");
  }));

$("autoexec-run").addEventListener("click", () => action("autoexec-run", "join-log",
  "Queuing auto-execute…", async () => {
    const result = await api("/api/autoexec/run", { method: "POST", body: "{}" });
    log("join-log", `Queued ${result.job.script} now.`, "ok");
  }, loadAutoExec));

$("autoexec-cancel").addEventListener("click", () => action("autoexec-cancel", "join-log",
  "Cancelling queued script…", async () => {
    const result = await api("/api/autoexec/cancel", { method: "POST", body: "{}" });
    log("join-log", result.cancelled ? "Auto-execute cancelled." : result.message,
      result.cancelled ? "ok" : "");
  }, loadAutoExec));

$("preview-btn").addEventListener("click", previewPlace);

$("join-btn").addEventListener("click", () => {
  const placeId = $("place-id").value.trim();
  if (!placeId) {
    log("join-log", "Enter a place id first.", "err");
    return;
  }
  return action("join-btn", "join-log", `Joining place ${placeId}…`, async () => {
    const result = await api("/api/join", {
      method: "POST",
      body: JSON.stringify({
        place_id: placeId,
        game_instance_id: $("job-id").value.trim() || undefined,
        wait: Number($("join-wait").value || 20),
      }),
    });
    log("join-log", result.joined
      ? `Joined ${placeId} on port ${result.instance?.port}.`
      : `Join delivered, not confirmed yet: ${pretty(result)}`, result.joined ? "ok" : "");
    if (result.autoexec) {
      log("join-log", `Auto-execute queued: ${result.autoexec.script} (${result.autoexec.trigger}).`, "ok");
    }
  }, loadAutoExec);
});

/* ---------------------------------------------------------------- execute */

function updateScriptMeta() {
  const bytes = new TextEncoder().encode($("script").value).length;
  $("script-meta").textContent = `${bytes} bytes`;
}
$("script").addEventListener("input", updateScriptMeta);
updateScriptMeta();

async function runScript(checkOnly) {
  const script = $("script").value;
  if (!script.trim()) {
    log("exec-out", "Write a script first.", "err");
    return;
  }
  const endpoint = checkOnly ? "/api/check" : "/api/execute";
  return action(["execute-btn", "check-btn"], "exec-out",
    checkOnly ? "Checking syntax…" : "Executing…", async () => {
    const result = await api(endpoint, {
      method: "POST",
      body: JSON.stringify({
        script,
        wait: Number($("exec-wait").value || 10),
        timeout: Number($("exec-timeout").value || 30),
        check: $("check-first").checked,
      }),
    });
    if (result.error === "syntax_error") {
      log("exec-out", pretty(result.check), "err");
      return;
    }
    log("exec-out", pretty(result), result.ok ? "ok" : "err");
  });
}

$("execute-btn").addEventListener("click", () => runScript(false));
$("check-btn").addEventListener("click", () => runScript(true));

/* ---------------------------------------------------------------- players */

function playerCard(instance) {
  const card = document.createElement("div");
  card.className = "player";

  const port = document.createElement("div");
  port.className = "port";
  port.textContent = `:${instance.port}`;

  const runtime = instance.runtime || {};
  const player = runtime.player || {};

  const who = document.createElement("div");
  const whoLabel = document.createElement("span");
  whoLabel.className = "label";
  whoLabel.textContent = "player";
  const whoValue = document.createElement("span");
  whoValue.className = "value";
  whoValue.textContent = player.name ? `${player.name}` : "not in game";
  who.append(whoLabel, whoValue);

  const where = document.createElement("div");
  const whereLabel = document.createElement("span");
  whereLabel.className = "label";
  whereLabel.textContent = "place";
  const whereValue = document.createElement("span");
  whereValue.className = "value";
  whereValue.textContent = runtime.place_id ? String(runtime.place_id) : "—";
  where.append(whereLabel, whereValue);

  const state = document.createElement("div");
  const stateLabel = document.createElement("span");
  stateLabel.className = "label";
  stateLabel.textContent = "state";
  const stateValue = document.createElement("span");
  stateValue.className = "value";
  stateValue.textContent = runtime.connected && runtime.loaded
    ? "connected"
    : runtime.loaded ? "loading" : "offline";
  state.append(stateLabel, stateValue);

  card.append(port, who, where, state);
  return card;
}

let playersBusy = false;
async function loadPlayers(force = false) {
  if (!force && !$("auto-refresh").checked) return;
  if (playersBusy) return;
  playersBusy = true;
  $("refresh-players").disabled = true;
  const placeId = $("place-id").value.trim();
  try {
    const data = await api(`/api/players${placeId ? `?place_id=${encodeURIComponent(placeId)}` : ""}`);
    const container = $("players");
    container.replaceChildren();
    if (!data.instances.length) {
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = "No Opiumware Player instances are running. Start the API and launch a Player.";
      container.appendChild(p);
    } else {
      data.instances.forEach((instance) => container.appendChild(playerCard(instance)));
    }
    $("players-meta").textContent = `${data.count} instance(s)`;
    renderServers(data.servers || { servers: [] });
    if (data.place_error) $("players-meta").textContent += ` · ${data.place_error.message}`;
  } catch (error) {
    $("players-meta").textContent = error.message;
  } finally {
    playersBusy = false;
    $("refresh-players").disabled = false;
  }
}

function renderServers(servers) {
  const container = $("servers");
  container.replaceChildren();
  const list = servers.servers || [];
  if (!list.length) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = "No public servers returned.";
    container.appendChild(p);
    return;
  }
  list.slice(0, 30).forEach((server) => {
    const card = document.createElement("div");
    card.className = "server";
    const count = document.createElement("b");
    count.textContent = `${server.playing ?? "?"}/${server.max_players ?? "?"}`;
    const ping = document.createElement("span");
    ping.textContent = `  ping ${server.ping ?? "?"}ms`;
    card.append(count, ping);
    container.appendChild(card);
  });
}

$("refresh-players").addEventListener("click", () => loadPlayers(true));

/* ------------------------------------------------------------------- boot */

const initial = (location.hash || "#setup").slice(1);
activate($(`tab-${initial}`) ? initial : "setup");
refreshState();
loadAutoExec();
setInterval(() => { if (!document.hidden) refreshState(); }, 6000);
setInterval(() => {
  if (!document.hidden && $("tab-players").classList.contains("active")) loadPlayers();
}, 4000);
window.addEventListener("hashchange", () => {
  const tab = location.hash.slice(1);
  if ($(`tab-${tab}`)) activate(tab);
});
