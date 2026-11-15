const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const staticPath = path.join(__dirname, "../whoisauto/static");
const source = fs.readFileSync(path.join(staticPath, "app.js"), "utf8");
const html = fs.readFileSync(path.join(staticPath, "index.html"), "utf8");
const tick = () => new Promise(setImmediate);
const response = (value, status = 200) => ({
  ok: status < 400, status, statusText: "Test response", json: async () => value,
});

function element() {
  const classes = new Set();
  return {
    value: "", checked: false, disabled: false, dataset: {}, textContent: "", className: "",
    listeners: {},
    classList: {
      toggle(name, on) { if (on) classes.add(name); else classes.delete(name); },
      contains(name) { return classes.has(name); },
    },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    setAttribute() {}, append() {}, appendChild() {}, replaceChildren() {}, focus() {},
    click() { return this.listeners.click?.({ currentTarget: this }); },
  };
}

async function panel() {
  const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map((match) => [match[1], element()]));
  const tabs = ["setup", "account", "join", "execute", "players"].map((tab) => {
    const button = element();
    button.dataset.tab = tab;
    return button;
  });
  elements.get("script").value = "print('test')";
  elements.get("auto-refresh").checked = true;
  const context = vm.createContext({
    document: {
      getElementById: (id) => elements.get(id), createElement: element,
      querySelectorAll: (selector) => selector === ".tab" ? tabs
        : [...elements.entries()].filter(([id]) => id.startsWith("tab-")).map(([, el]) => el),
      hidden: false,
    },
    history: { replaceState() {} }, location: { hash: "#setup" },
    window: { addEventListener() {} }, console: { error() {} },
    AbortController, TextEncoder, setTimeout, clearTimeout, setInterval: () => 0,
    fetch: async (url) => response(url === "/api/state"
      ? { install: {}, service: {}, account: { signed_in: false } }
      : { config: { script: "", enabled: false } }),
  });
  vm.runInContext(source, context);
  await tick();
  return { context, elements, tabs };
}

test("repeated Execute clicks dispatch one script and restore controls", async () => {
  const { context, elements } = await panel();
  let complete;
  let calls = 0;
  context.fetch = () => {
    calls++;
    return new Promise((resolve) => { complete = resolve; });
  };
  const first = context.runScript(false);
  await context.runScript(false);
  assert.equal(calls, 1);
  assert.equal(elements.get("execute-btn").disabled, true);
  assert.equal(elements.get("check-btn").disabled, true);
  complete(response({ ok: true, job: { status: "completed" } }));
  await first;
  assert.equal(elements.get("execute-btn").disabled, false);
  assert.equal(elements.get("check-btn").disabled, false);
});

test("failed execution shows an error and restores controls", async () => {
  const { context, elements } = await panel();
  context.fetch = async () => response({ message: "Player disconnected" }, 503);
  await context.runScript(false);
  assert.match(elements.get("exec-out").textContent, /Player disconnected/);
  assert.match(elements.get("exec-out").className, /err/);
  assert.equal(elements.get("execute-btn").disabled, false);
});

test("polling does not overlap pending state requests", async () => {
  const { context } = await panel();
  let complete;
  let calls = 0;
  context.fetch = () => {
    calls++;
    return new Promise((resolve) => { complete = resolve; });
  };
  const first = context.refreshState();
  await context.refreshState();
  assert.equal(calls, 1);
  complete(response({ service: {}, install: {}, account: { signed_in: false } }));
  await first;
});

test("malformed successful responses fail visibly", async () => {
  const { context } = await panel();
  context.fetch = async () => response([]);
  await assert.rejects(context.api("/api/state"), /Invalid response/);
});

test("cancel is available only before dispatch", async () => {
  const { context, elements } = await panel();
  context.renderAutoExec({ job: { status: "waiting" } });
  assert.equal(elements.get("autoexec-cancel").disabled, false);
  context.renderAutoExec({ job: { status: "running" } });
  assert.equal(elements.get("autoexec-cancel").disabled, true);
});

test("opening Players fetches once even with auto refresh off", async () => {
  const { context, elements } = await panel();
  elements.get("auto-refresh").checked = false;
  const calls = [];
  context.fetch = async (url) => { calls.push(url); return response({ instances: [], count: 0 }); };
  context.activate("players");
  await tick();
  assert.deepEqual(calls, ["/api/players"]);
});

test("loading auto-execute does not overwrite unsaved edits", async () => {
  const { context, elements } = await panel();
  const editor = elements.get("autoexec-script");
  editor.value = "print('unsaved')";
  editor.listeners.input();
  context.renderAutoExec({ config: { script: "print('old saved')" } });
  assert.equal(editor.value, "print('unsaved')");
});

test("editing during an auto-execute save keeps the newer text", async () => {
  const { context, elements } = await panel();
  let complete;
  context.fetch = () => new Promise((resolve) => { complete = resolve; });
  const editor = elements.get("autoexec-script");
  editor.value = "print('first')";
  editor.listeners.input();
  const save = elements.get("autoexec-save").click();
  editor.value = "print('newer')";
  editor.listeners.input();
  complete(response({ config: { script: "print('first')", enabled: true, name: "auto.luau", bytes: 14 } }));
  await save;
  assert.equal(editor.value, "print('newer')");
});
