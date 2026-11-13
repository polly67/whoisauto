const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const init = { headers: { "Content-Type": "application/json" }, ...options };
  const response = await fetch(path, init);
  let data;
  try {
    data = await response.json();
  } catch {
    data = { ok: false, error: "bad_response", message: response.statusText };
  }
  if (!response.ok) {
    const error = new Error(data.message || response.statusText);
    error.data = data;
    error.status = response.status;
    throw error;
  }
  return data;
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
  });
  document.querySelectorAll(".panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `tab-${tab}`);
  });
  if (tab === "players") loadPlayers();
}

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => {
    activate(button.dataset.tab);
    history.replaceState(null, "", `#${button.dataset.tab}`);
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

async function refreshState() {
  try {
    const state = await api("/api/state");
    renderInstall(state.install || {});
    renderService(state.service || {});
    renderAccount(state.account || { signed_in: false });
  } catch (error) {
    pill($("pill-api"), false, "api: panel unreachable");
    console.error(error);
  }
}

/* ------------------------------------------------------------------ setup */

$("install-btn").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  log("setup-log", "Installing Opiumware and the patched Roblox app into the opom root…");
  try {
    const result = await api("/api/install", {
      method: "POST",
      body: JSON.stringify({
        editor_source: $("editor-source").value.trim(),
        roblox_source: $("roblox-source").value.trim(),
        launch: $("launch-after").checked,
      }),
    });
    log("setup-log", pretty(result), result.ok ? "ok" : "err");
  } catch (error) {
    log("setup-log", `${error.data?.error || "error"}: ${error.message}`, "err");
  } finally {
    button.disabled = false;
    refreshState();
  }
});

async function serviceAction(path, label) {
  log("setup-log", `${label}…`);
  try {
    const result = await api(path, { method: "POST", body: "{}" });
    log("setup-log", pretty(result), "ok");
  } catch (error) {
    log("setup-log", `${error.data?.error || "error"}: ${error.message}`, "err");
  } finally {
    refreshState();
  }
}

$("start-api").addEventListener("click", () => serviceAction("/api/service/start", "Starting opom"));
$("stop-api").addEventListener("click", () => serviceAction("/api/service/stop", "Stopping opom"));

/* ---------------------------------------------------------------- account */

$("save-token").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const token = $("token").value.trim();
  if (!token) {
    log("account-log", "Paste a token first.", "err");
    return;
  }
  button.disabled = true;
  log("account-log", "Verifying token with Roblox…");
  try {
    const result = await api("/api/account", {
      method: "POST",
      body: JSON.stringify({ token }),
    });
    $("token").value = "";
    log("account-log", `Signed in as ${result.account.user.name} (${result.account.user.id}).`, "ok");
    renderAccount(result.account);
  } catch (error) {
    log("account-log", `${error.data?.error || "error"}: ${error.message}`, "err");
  } finally {
    button.disabled = false;
  }
});

$("clear-token").addEventListener("click", async () => {
  try {
    const result = await api("/api/account", { method: "DELETE" });
    renderAccount(result.account);
    log("account-log", "Token removed from local state.", "ok");
  } catch (error) {
    log("account-log", error.message, "err");
  }
});

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

$("preview-btn").addEventListener("click", previewPlace);

$("join-btn").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const placeId = $("place-id").value.trim();
  if (!placeId) {
    log("join-log", "Enter a place id first.", "err");
    return;
  }
  button.disabled = true;
  log("join-log", `Joining place ${placeId}…`);
  try {
    const result = await api("/api/join", {
      method: "POST",
      body: JSON.stringify({
        place_id: Number(placeId),
        game_instance_id: $("job-id").value.trim() || undefined,
        wait: Number($("join-wait").value || 20),
      }),
    });
    log("join-log", result.joined
      ? `Joined ${placeId} on port ${result.instance?.port}.`
      : `Join delivered, not confirmed yet: ${pretty(result)}`, result.joined ? "ok" : "");
  } catch (error) {
    log("join-log", `${error.data?.error || "error"}: ${error.message}`, "err");
  } finally {
    button.disabled = false;
  }
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
  log("exec-out", checkOnly ? "Checking syntax…" : "Executing…");
  try {
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
  } catch (error) {
    log("exec-out", `${error.data?.error || "error"}: ${error.message}`, "err");
  }
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

async function loadPlayers(force = false) {
  if (!force && !$("auto-refresh").checked) return;
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
    if (data.servers) renderServers(data.servers);
  } catch (error) {
    $("players-meta").textContent = error.message;
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
if ($(`tab-${initial}`)) activate(initial);
refreshState();
setInterval(refreshState, 6000);
setInterval(() => {
  if ($("tab-players").classList.contains("active")) loadPlayers();
}, 4000);
