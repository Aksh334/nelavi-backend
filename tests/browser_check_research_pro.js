"use strict";
// TEST-ONLY real-browser check of Section 6 (Research Pro). Not part of the application.
//
// Starts tests/browser_fake_server.py (the real demo, a FAKE Tavily transport, loopback-only network guard),
// starts headless Chrome with a throwaway profile in .browser_tmp/, drives the real page over the DevTools
// protocol (Node's built-in WebSocket; nothing is installed) and prints one PASS/FAIL line per scenario.
//
// Run from the repository root:  node tests/browser_check_research_pro.js ["<path to chrome.exe>"]
// Nothing is sent to Tavily or anywhere else. Provider answers are SYNTHETIC and come from the fake server.

const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const ROOT = process.cwd();
// Throwaway Chrome profile only. Removed after a PASSED run, left in place after any other outcome.
const TMP = path.join(ROOT, ".browser_tmp");
const RUN = path.join(TMP, "run-" + Date.now());   // every run writes only inside its own sub-folder
const PROFILE = path.join(RUN, "profile");
// Retained evidence: one new, uniquely named directory per run. This script never deletes or overwrites any of it.
const EVIDENCE_ROOT = path.join(ROOT, ".browser_evidence");
const EVID = path.join(EVIDENCE_ROOT, "section6-browser-" + new Date().toISOString().replace(/[:.]/g, "-") + "-pid" + process.pid);
const PROVIDER_LOG = path.join(EVID, "provider_calls.jsonl");   // written by tests/browser_fake_server.py
const RESULTS = path.join(EVID, "results.json");
const NETWORK_FILE = path.join(EVID, "page_requests.json");
const STATUS_FILE = path.join(EVID, "STATUS.txt");
const EXPECTED_SCENARIOS = 13;
// What an earlier run of this script left directly in .browser_tmp, plus the sub-folders written by runs of this script.
const LEFTOVERS = ["profile", "provider_calls.jsonl", "results.json"];
const isOwnEntry = (name) => LEFTOVERS.includes(name) || /^run-\d+$/.test(name);

const SAFEGUARDS = [
  "Launcher process: a Python-level guard makes connect, connect_ex, getaddrinfo, gethostbyname and gethostbyaddr refuse anything except 127.0.0.1, ::1 and localhost; its self-test of five refused probes must print ISOLATION_SELFTEST_OK before the server starts.",
  "Fake provider: the Tavily transport is a Python function in tests/browser_fake_server.py. The real transport is not used and the adapter is given a made-up key string, never the real environment variable.",
  "Chrome flags: --host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1; --proxy-server=http://127.0.0.1:1; background networking, sync, component update, domain reliability and phishing detection disabled.",
  "The demo server is bound to 127.0.0.1 on an OS-assigned port; the launcher and this script refuse port 8765.",
];
const LIMITATIONS = [
  "These are configured safeguards, one probe result and Chrome's own request log for the single test page. They are NOT independent packet-level proof: there was no firewall log or packet capture.",
  "The launcher guard covers only that Python process and only Python-level socket calls.",
  "Browser-internal traffic outside the test page is not observed here; it is limited by flags only.",
  "That nothing listens on 127.0.0.1:1 (the dead proxy address) is assumed and was not checked.",
  "The timeout scenario uses a SIMULATED clock (the fake provider adds 50 s to the server clock per fake poll). Every other scenario runs in real time.",
  "The provider is a fake. Nothing here shows how the real Tavily service behaves.",
  "The stale-poll takeover is SIMULATED by advancing the page's internal run counter.",
];
// Patterns are reported by label only, so the evidence never contains the very text it searches for.
const SECRET_PATTERNS = [
  { label: "authorization-bearer-value", re: /Bearer\s+\S/i },
  { label: "tavily-key-prefix", re: /tvly-/i },
  { label: "made-up-test-key-string", re: /fake-key-not-real/i },
  { label: "nvidia-key-prefix", re: /nvapi-/i },
  { label: "sk-style-key", re: /sk-[A-Za-z0-9]{20,}/ },
  { label: "key-variable-assignment", re: /TAVILY_API_KEY\s*[=:]/i },
];

let startedAt = null;
let chromeProduct = null;
let devtoolsPort = null;
let isolationProbe = null;
let evidenceScan = null;
const CHROME = process.argv[2] || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const LABEL = "UNREVIEWED AI-GENERATED RESEARCH \u2014 NOT VERIFIED AGRICULTURAL ADVICE.";
const COMPLETE_TEXT = "SYNTHETIC mocked research text.\nSecond line.";
const HTML_TEXT = '<b>BOLD</b> <img src="x" onerror="window.__xss=1"> <script>window.__xss=1</script> &amp; end';
const APPROVED_HREFS = ["https://www.icar.org.in/guide-1", "https://krishi.icar.org.in/page"];
const REJECTED_FRAGMENTS = ["icar.org.in.evil.example", "noticar", "evil.example", "javascript:", "http://icar.org.in/plain"];
const PRIVATE_VALUES = ["Tomato", "Karnataka", "Flowering"];
const PROBE_ERRORS = ["ERR_PROXY_CONNECTION_FAILED", "ERR_NAME_NOT_RESOLVED", "ERR_TUNNEL_CONNECTION_FAILED",
  "ERR_CONNECTION_REFUSED", "ERR_NAME_RESOLUTION_FAILED"];

const out = [];
function say(line) { console.log(line); out.push(line); }

// ------------------------------------------------------------------ processes
let server = null;
let chrome = null;
let serverLines = [];
let serverErr = "";
let chromeExit = null;

async function startServer() {
  server = spawn("python", ["-B", "tests/browser_fake_server.py", "--log", PROVIDER_LOG],
    { cwd: ROOT, stdio: ["pipe", "pipe", "pipe"] });
  let buffer = "";
  server.stdout.on("data", (d) => {
    buffer += d.toString();
    let i;
    while ((i = buffer.indexOf("\n")) >= 0) { serverLines.push(buffer.slice(0, i).trim()); buffer = buffer.slice(i + 1); }
  });
  server.stderr.on("data", (d) => { serverErr += d.toString(); });
  const exited = new Promise((r) => server.on("exit", (code) => r(code)));
  for (let i = 0; i < 150; i++) {
    if (serverLines.some((l) => l.startsWith("READY port="))) break;
    if (serverLines.includes("ISOLATION_SELFTEST_FAILED")) throw new Error("network isolation self-test FAILED in the launcher");
    if (await Promise.race([exited, sleep(100).then(() => null)]) !== null) throw new Error("launcher exited early: " + serverErr);
  }
  assert.ok(serverLines.includes("ISOLATION_SELFTEST_OK"), "launcher did not report ISOLATION_SELFTEST_OK");
  const ready = serverLines.find((l) => l.startsWith("READY port="));
  assert.ok(ready, "launcher never became ready: " + serverErr);
  const port = Number(ready.split("=")[1]);
  assert.ok(port > 0 && port !== 8765, "unexpected port " + port);
  return port;
}

async function startChrome() {
  const args = [
    "--headless=new", "--remote-debugging-port=0", "--remote-allow-origins=*", "--user-data-dir=" + PROFILE,
    "--no-first-run", "--no-default-browser-check", "--disable-background-networking", "--disable-component-update",
    "--disable-sync", "--disable-default-apps", "--disable-extensions", "--disable-breakpad", "--no-pings",
    "--metrics-recording-only", "--disable-domain-reliability", "--disable-client-side-phishing-detection",
    // Isolation: every name except loopback fails to resolve, and anything that is not loopback is sent to a
    // proxy address where nothing listens, so it fails instead of leaving the machine.
    '--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1', "--proxy-server=http://127.0.0.1:1",
    "--window-size=1100,900", "about:blank",
  ];
  chrome = spawn(CHROME, args, { stdio: "ignore" });
  const failed = new Promise((_, rej) => chrome.on("error", (e) => rej(new Error("cannot run Chrome: " + e.code + " " + e.message))));
  failed.catch(() => {});  // a late error must not crash the script; the race below still sees it
  chrome.on("exit", (code) => { chromeExit = code; });
  const portFile = path.join(PROFILE, "DevToolsActivePort");
  for (let i = 0; i < 200; i++) {
    await Promise.race([sleep(100), failed]);
    if (fs.existsSync(portFile)) {
      const lines = fs.readFileSync(portFile, "utf8").split(/\r?\n/);
      if (lines[0] && lines[1]) return { port: Number(lines[0]), wsPath: lines[1] };
    }
    if (chromeExit !== null) throw new Error("Chrome exited early with code " + chromeExit);
  }
  throw new Error("Chrome never wrote DevToolsActivePort");
}

// ------------------------------------------------------------------ DevTools protocol client
class CDP {
  constructor(url) {
    this.ws = new WebSocket(url);
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = [];
    this.ws.addEventListener("message", (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { res, rej } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        if (msg.error) rej(new Error(msg.error.message)); else res(msg.result);
      } else if (msg.method) {
        this.listeners.forEach((fn) => fn(msg));
      }
    });
  }
  open() {
    return new Promise((res, rej) => {
      this.ws.addEventListener("open", () => res());
      this.ws.addEventListener("error", () => rej(new Error("DevTools websocket error")));
    });
  }
  send(method, params, sessionId) {
    const id = this.nextId++;
    const message = { id, method, params: params || {} };
    if (sessionId) message.sessionId = sessionId;
    return new Promise((res, rej) => { this.pending.set(id, { res, rej }); this.ws.send(JSON.stringify(message)); });
  }
  close() { try { this.ws.close(); } catch (e) { /* already closed */ } }
}

const network = [];       // every request the page made
const consoleProblems = [];
let cdp = null;
let session = null;
let sitePort = 0;
const origin = () => "http://127.0.0.1:" + sitePort;

async function evalJS(expression) {
  const r = await cdp.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true }, session);
  if (r.exceptionDetails) {
    throw new Error("page exception: " + ((r.exceptionDetails.exception && r.exceptionDetails.exception.description) || r.exceptionDetails.text));
  }
  return r.result.value;
}

async function waitFor(expression, timeoutMs, label) {
  const start = Date.now();
  let last;
  while (Date.now() - start < timeoutMs) {
    last = await evalJS(expression);
    if (last) return last;
    await sleep(150);
  }
  throw new Error("timed out waiting for " + label + " (last value: " + JSON.stringify(last) + ")");
}

const byId = (id) => "document.getElementById(" + JSON.stringify(id) + ")";

async function loadPage(reload) {
  const before = await evalJS("performance.timeOrigin").catch(() => 0);
  if (reload) await cdp.send("Page.reload", { ignoreCache: true }, session);
  else await cdp.send("Page.navigate", { url: origin() + "/?load=" + Date.now() }, session);
  await waitFor("performance.timeOrigin !== " + JSON.stringify(before) + " && document.readyState === 'complete' && " +
    byId("rs-status") + ".textContent.startsWith('Ready.')", 20000, "the page to load and Section 6 to become ready");
}

async function typeInto(id, text) {
  await evalJS("(() => { const e = " + byId(id) + "; e.focus(); e.select(); return true; })()");
  await cdp.send("Input.insertText", { text }, session);
}

async function clickReal(id) {
  const rect = await evalJS("(() => { const e = " + byId(id) + "; e.scrollIntoView({ block: 'center' }); " +
    "const r = e.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2, disabled: !!e.disabled }; })()");
  for (const type of ["mousePressed", "mouseReleased"]) {
    await cdp.send("Input.dispatchMouseEvent", { type, x: rect.x, y: rect.y, button: "left", clickCount: 1 }, session);
  }
  return rect;
}

async function sample() {
  return evalJS("({ status: " + byId("rs-status") + ".textContent, cls: " + byId("rs-status") + ".className, " +
    "disabled: " + byId("rs-submit") + ".disabled, hidden: " + byId("rp-result") + ".hidden })");
}

async function sampleUntil(substring, timeoutMs) {
  const samples = [];
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const s = await sample();
    if (!samples.length || samples[samples.length - 1].status !== s.status) samples.push(s);
    if (s.status.includes(substring)) return samples;
    await sleep(150);
  }
  throw new Error("timed out waiting for status containing " + JSON.stringify(substring) + "; last: " + JSON.stringify(samples[samples.length - 1]));
}

function providerCalls() {
  if (!fs.existsSync(PROVIDER_LOG)) return [];
  return fs.readFileSync(PROVIDER_LOG, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l));
}

const sinceMark = (mark) => network.slice(mark);
const posts = (list, suffix) => list.filter((r) => r.method === "POST" && r.url.endsWith(suffix));
const statusGets = (list) => list.filter((r) => r.method === "GET" && r.url.includes("/api/research-pro/status"));

async function newQuestion(text) {
  await typeInto("rs-question", text);
  await clickReal("rs-submit");
}

// ------------------------------------------------------------------ scenarios
const results = [];

// PASSED only if every expected scenario ran and passed and nothing aborted; FAILED if any scenario failed (or the
// evidence scan found a credential); otherwise INCOMPLETE (aborted, crashed or fewer scenarios than expected).
function outcome(fatal) {
  if (results.some((r) => !r.pass) || (evidenceScan && evidenceScan.hits.length)) return "FAILED";
  if (fatal || results.length !== EXPECTED_SCENARIOS) return "INCOMPLETE";
  return "PASSED";
}

// Written at the start, after every scenario and at the end, so a crash still leaves evidence marked INCOMPLETE.
// It stores outcomes and request metadata only: no header values, no environment values, no credentials.
function persist(state) {
  try {
    const doc = {
      schema: 1, status: state, started_at: startedAt, written_at: new Date().toISOString(),
      expected_scenarios: EXPECTED_SCENARIOS, executed_scenarios: results.length,
      browser: chromeProduct, headless: true, devtools_port_loopback: devtoolsPort,
      server: { host: "127.0.0.1", port: sitePort, port_8765_used: sitePort === 8765 },
      isolation_probe: isolationProbe, safeguards: SAFEGUARDS, limitations: LIMITATIONS,
      simulated_timing: "timeout scenario only (+50 s per fake poll); all other scenarios use real time",
      scenarios: results, console_problems: consoleProblems, evidence_scan: evidenceScan, output: out,
    };
    fs.writeFileSync(RESULTS, JSON.stringify(doc, null, 2));
    fs.writeFileSync(STATUS_FILE, state + "\n");
    fs.writeFileSync(NETWORK_FILE, JSON.stringify(network.map((r) => ({
      method: r.method, url: r.url,
      // the only request body kept is the research start body, which contains the typed question and nothing else
      post_body: r.method === "POST" && r.url.endsWith("/api/research-pro/start") ? r.postData : null,
    })), null, 2));
  } catch (e) {
    console.log("could not write evidence: " + e.message);
  }
}

async function scenario(name, note, fn) {
  try {
    await fn();
    results.push({ name, note, pass: true });
    say("PASS - " + name + "  [" + note + "]");
  } catch (e) {
    results.push({ name, note, pass: false, error: String(e && e.message ? e.message : e) });
    say("FAIL - " + name + "\n       " + (e && e.message ? e.message : e));
  }
  persist("INCOMPLETE");
}

async function main() {
  // Evidence first: a new unique directory (mkdir without recursive fails if it already exists, so nothing is overwritten).
  fs.mkdirSync(EVIDENCE_ROOT, { recursive: true });
  fs.mkdirSync(EVID);
  startedAt = new Date().toISOString();
  persist("INCOMPLETE");
  say("evidence directory (kept): " + path.relative(ROOT, EVID));

  if (fs.existsSync(TMP)) {
    const foreign = fs.readdirSync(TMP).filter((n) => !isOwnEntry(n));
    assert.deepStrictEqual(foreign, [], ".browser_tmp holds files this test did not create; refusing to use it: " + foreign.join(", "));
  }
  fs.mkdirSync(PROFILE, { recursive: true });

  sitePort = await startServer();
  say("launcher: the network-guard self-test reported OK; fake server on 127.0.0.1:" + sitePort + " (OS-assigned)");

  const dev = await startChrome();
  devtoolsPort = dev.port;
  cdp = new CDP("ws://127.0.0.1:" + dev.port + dev.wsPath);
  await cdp.open();
  const version = await cdp.send("Browser.getVersion");
  chromeProduct = version.product;
  say("browser: " + version.product + " (headless), DevTools on loopback port " + dev.port);

  const created = await cdp.send("Target.createTarget", { url: "about:blank" });
  session = (await cdp.send("Target.attachToTarget", { targetId: created.targetId, flatten: true })).sessionId;
  cdp.listeners.push((msg) => {
    if (msg.sessionId !== session) return;
    const p = msg.params;
    if (msg.method === "Network.requestWillBeSent") {
      network.push({ method: p.request.method, url: p.request.url, postData: p.request.postData || null });
    } else if (msg.method === "Runtime.exceptionThrown") {
      consoleProblems.push({ kind: "exception", text: (p.exceptionDetails.exception && p.exceptionDetails.exception.description) || p.exceptionDetails.text });
    } else if (msg.method === "Runtime.consoleAPICalled" && p.type === "error") {
      consoleProblems.push({ kind: "console.error", text: p.args.map((a) => a.value || a.description).join(" ") });
    } else if (msg.method === "Log.entryAdded" && p.entry.level === "error") {
      consoleProblems.push({ kind: "log", text: p.entry.text, url: p.entry.url || "" });
    }
  });
  for (const domain of ["Page", "Runtime", "Network", "Log"]) await cdp.send(domain + ".enable", {}, session);
  await cdp.send("Emulation.setDeviceMetricsOverride", { width: 1100, height: 900, deviceScaleFactor: 1, mobile: false }, session);

  // ---- network isolation of the browser itself, before the page is used
  const probe = await cdp.send("Page.navigate", { url: "http://isolation-probe.invalid/" }, session);
  isolationProbe = { url: "http://isolation-probe.invalid/", error_text: probe.errorText || null };
  assert.ok(probe.errorText && PROBE_ERRORS.some((e) => probe.errorText.includes(e)),
    "isolation probe did not fail as expected: " + JSON.stringify(probe));
  say("isolation probe: navigating to http://isolation-probe.invalid/ failed with " + probe.errorText +
    ". Observed only this probe result, the configured safeguards and the page's logged requests (see results.json); " +
    "these are NOT independent packet-level proof.");
  const probeEnd = network.length;

  await scenario("Section 6 loads, is visible and states the real mode", "REAL BROWSER; mocked server state", async () => {
    await loadPage(false);
    const page = await evalJS("({ heading: document.querySelector('#research h2').textContent, " +
      "height: " + byId("research") + ".getBoundingClientRect().height, notice: " + byId("rs-notice") + ".textContent, " +
      "banner: " + byId("banner") + ".textContent, button: " + byId("rs-submit") + ".textContent, " +
      "disabled: " + byId("rs-submit") + ".disabled, hidden: " + byId("rp-result") + ".hidden, " +
      "maxLength: " + byId("rs-question") + ".maxLength })");
    assert.strictEqual(page.heading, "6. Research (advisory)");
    assert.ok(page.height > 50, "Section 6 is not visible");
    for (const text of ["REAL SERVICE", "icar.org.in", "120 seconds", "15 to 250 credits", "unreviewed"]) {
      assert.ok(page.notice.includes(text), "notice lacks " + text);
    }
    assert.ok(page.banner.startsWith("MIXED MODE."), page.banner);
    assert.strictEqual(page.button, "Start research task");
    assert.strictEqual(page.disabled, false);
    assert.strictEqual(page.hidden, true);
    assert.strictEqual(page.maxLength, 200);
  });

  await scenario("completed task: running state, label, text, approved links only", "REAL BROWSER; MOCKED provider", async () => {
    await loadPage(false);
    await typeInto("rs-question", "complete soil sampling");
    await clickReal("rs-submit");
    const samples = await sampleUntil("Done.", 45000);
    const running = samples.find((s) => s.status.startsWith("Research task running"));
    assert.ok(running, "never saw the running state: " + JSON.stringify(samples));
    assert.strictEqual(running.disabled, true, "button was not disabled while running");
    assert.strictEqual(running.hidden, true, "a result was shown while the task was running");
    const final = await evalJS("({ label: " + byId("rp-label") + ".textContent, text: " + byId("rp-text") + ".textContent, " +
      "meta: " + byId("rp-meta") + ".textContent, note: " + byId("rp-sources-note") + ".textContent, " +
      "heading: " + byId("rp-sources-heading") + ".textContent, hidden: " + byId("rp-result") + ".hidden, " +
      "visible: " + byId("rp-result") + ".getBoundingClientRect().height > 0, shown: " + byId("rp-result") + ".innerText, " +
      "links: Array.from(document.querySelectorAll('#rp-sources a')).map((a) => ({ href: a.href, rel: a.rel, target: a.target })), " +
      "caveats: Array.from(document.querySelectorAll('#rp-caveats li')).map((l) => l.textContent), " +
      "button: " + byId("rs-submit") + ".disabled })");
    assert.strictEqual(final.hidden, false);
    assert.ok(final.visible, "the result is not visible");
    assert.strictEqual(final.label, LABEL);
    assert.strictEqual(final.text, COMPLETE_TEXT);
    assert.ok(final.meta.includes("Question: complete soil sampling"));
    assert.deepStrictEqual(final.links.map((l) => l.href), APPROVED_HREFS);
    for (const l of final.links) {
      assert.ok(["noopener", "noreferrer", "nofollow"].every((w) => l.rel.includes(w)), l.rel);
      assert.strictEqual(l.target, "_blank");
    }
    for (const bad of REJECTED_FRAGMENTS) assert.ok(!final.shown.includes(bad), "rejected link text is displayed: " + bad);
    assert.ok(final.note.includes("6 returned source(s) were not on the approved domain"), final.note);
    assert.ok(final.heading.includes("icar.org.in"));
    assert.ok(final.caveats.some((c) => c.includes("may use or mention other sources") || c.includes("may use or mention")), JSON.stringify(final.caveats));
    assert.strictEqual(final.button, false, "button should be usable again");
  });

  await scenario("Section 1 values are read back from the page, then only the question is sent", "REAL BROWSER; MOCKED provider", async () => {
    await loadPage(false);
    const entered = { "user-crop": "Tomato", "user-region": "Karnataka", "user-stage": "Flowering" };
    const question = "complete privacy check";
    for (const [id, value] of Object.entries(entered)) await typeInto(id, value);
    await typeInto("rs-question", question);
    // Read the REAL field values back from the page. This fails the scenario if any field was not actually filled.
    const readBack = async () => evalJS("({ crop: " + byId("user-crop") + ".value, region: " + byId("user-region") + ".value, " +
      "stage: " + byId("user-stage") + ".value, question: " + byId("rs-question") + ".value })");
    const before = await readBack();
    assert.deepStrictEqual(before, { crop: "Tomato", region: "Karnataka", stage: "Flowering", question },
      "a Section 1 field or the question was not actually filled: " + JSON.stringify(before));
    const mark = network.length;
    const callsBefore = providerCalls().length;
    await clickReal("rs-submit");
    await sampleUntil("Done.", 45000);
    // The values must still be in the fields after the submit: they were there, and the page simply did not send them.
    const after = await readBack();
    assert.deepStrictEqual(after, before, "the fields changed during the submit: " + JSON.stringify(after));
    const mine = sinceMark(mark);
    const startRequests = posts(mine, "/api/research-pro/start");
    assert.strictEqual(startRequests.length, 1);
    assert.strictEqual(startRequests[0].postData, JSON.stringify({ question }));
    assert.deepStrictEqual(Object.keys(JSON.parse(startRequests[0].postData)), ["question"]);
    assert.deepStrictEqual(mine.filter((r) => r.method !== "GET").map((r) => r.method + " " + new URL(r.url).pathname),
      ["POST /api/research-pro/start"], "the page sent some other non-GET request");
    const everything = JSON.stringify(mine).toLowerCase();
    for (const value of PRIVATE_VALUES) {
      assert.ok(!everything.includes(value.toLowerCase()), "the page's requests contain " + value);
      assert.ok(!everything.includes(encodeURIComponent(value).toLowerCase()), "the page's requests contain " + value);
    }
    const provider = providerCalls().slice(callsBefore);
    const post = provider.find((c) => c.method === "POST");
    assert.ok(post, "the fake provider saw no start request");
    assert.deepStrictEqual(Object.keys(post.body).sort(),
      ["citation_format", "include_domains", "input", "model", "output_length", "stream"]);
    assert.deepStrictEqual([post.body.input, post.body.model, post.body.stream, post.body.include_domains],
      [question, "pro", false, ["icar.org.in"]]);
    for (const value of PRIVATE_VALUES) {
      assert.ok(!JSON.stringify(provider).toLowerCase().includes(value.toLowerCase()), "the fake provider received " + value);
    }
    assert.ok(provider.every((c) => c.authorization_is_bearer === true));
  });

  await scenario("generated HTML is shown as text and nothing executes", "REAL BROWSER; MOCKED provider", async () => {
    await loadPage(false);
    const mark = network.length;
    await newQuestion("html injection test");
    await sampleUntil("Done.", 45000);
    const dom = await evalJS("({ children: " + byId("rp-text") + ".children.length, text: " + byId("rp-text") + ".textContent, " +
      "elements: document.querySelectorAll('#rp-text b, #rp-text img, #rp-text script').length, " +
      "xss: typeof window.__xss, inlineHandlers: document.querySelectorAll('#rp-result [onerror]').length })");
    assert.strictEqual(dom.children, 0, "the text contains child elements");
    assert.strictEqual(dom.text, HTML_TEXT);
    assert.strictEqual(dom.elements, 0);
    assert.strictEqual(dom.xss, "undefined", "injected script or handler ran");
    assert.strictEqual(dom.inlineHandlers, 0);
    assert.ok(!sinceMark(mark).some((r) => r.url === origin() + "/x"), "the injected <img src=x> was requested");
  });

  await scenario("failed task shows a clear state, no text and no links", "REAL BROWSER; MOCKED provider", async () => {
    await loadPage(false);
    await newQuestion("fail this one");
    const samples = await sampleUntil("reported that the task failed", 45000);
    const last = samples[samples.length - 1];
    assert.ok(last.status.includes("No result was produced."));
    assert.ok(last.cls.includes("bad"));
    assert.strictEqual(last.hidden, true);
    await waitFor("!" + byId("rs-submit") + ".disabled", 5000, "the button to be usable again");
  });

  await scenario("malformed provider answer is an error, never a result", "REAL BROWSER; MOCKED provider", async () => {
    await loadPage(false);
    await newQuestion("malformed reply");
    const samples = await sampleUntil("(code: response_malformed)", 45000);
    const last = samples[samples.length - 1];
    assert.ok(last.status.includes("No result was produced."));
    assert.strictEqual(last.hidden, true);
  });

  await scenario("rate limit: the page shows the wait, then completes", "REAL BROWSER; MOCKED provider (Retry-After 6)", async () => {
    await loadPage(false);
    await newQuestion("ratelimit check");
    const samples = await sampleUntil("Done.", 60000);
    assert.ok(samples.some((s) => s.status.includes("asked us to wait 6 seconds")), JSON.stringify(samples.map((s) => s.status)));
    const gets = providerCalls().filter((c) => c.scenario === "ratelimit" && c.method === "GET");
    assert.strictEqual(gets.length, 2);
    assert.deepStrictEqual(gets.map((g) => g.reply_status), [429, 200]);
  });

  await scenario("timeout: the task ends as timed out and shows no result", "REAL BROWSER; MOCKED provider; ACCELERATED CLOCK (+50 s per fake poll)", async () => {
    await loadPage(false);
    await newQuestion("timeout case");
    const samples = await sampleUntil("did not finish within 120 seconds", 90000);
    const last = samples[samples.length - 1];
    assert.ok(last.status.includes("No result was produced."));
    assert.strictEqual(last.hidden, true);
    assert.ok(samples.some((s) => s.status.startsWith("Research task running")));
    const jumps = providerCalls().filter((c) => c.scenario === "timeout" && c.clock_jump_seconds);
    assert.ok(jumps.length >= 2, "expected the fake clock to have been moved forward");
  });

  await scenario("duplicate start: two clicks and two direct calls start one task", "REAL BROWSER; MOCKED provider", async () => {
    await loadPage(false);
    await typeInto("rs-question", "complete duplicate");
    const mark = network.length;
    const first = await clickReal("rs-submit");
    const second = await clickReal("rs-submit");
    await evalJS("Promise.all([submitResearchPro(null), submitResearchPro(null)]).then(() => true)");
    assert.strictEqual(first.disabled, false);
    assert.strictEqual(second.disabled, true, "the button was not disabled after the first click");
    await sampleUntil("Done.", 45000);
    assert.strictEqual(posts(sinceMark(mark), "/api/research-pro/start").length, 1, "more than one start request left the page");
    const provider = providerCalls().filter((c) => c.method === "POST" && c.body && c.body.input === "complete duplicate");
    assert.strictEqual(provider.length, 1, "the fake provider received more than one task");
  });

  await scenario("reloading the page drops the old task; a new task shows only its own result", "REAL BROWSER; MOCKED provider", async () => {
    await loadPage(false);
    await newQuestion("complete reload one");
    await waitFor(byId("rs-status") + ".textContent.startsWith('Research task running')", 10000, "the running state");
    await loadPage(true);
    const mark = network.length;
    const fresh = await sample();
    assert.ok(fresh.status.startsWith("Ready."), fresh.status);
    assert.strictEqual(fresh.hidden, true);
    await sleep(12000);
    const later = await sample();
    assert.ok(later.status.startsWith("Ready."), "the old task changed the new page: " + later.status);
    assert.strictEqual(later.hidden, true);
    assert.strictEqual(statusGets(sinceMark(mark)).length, 0, "the new page polled a task it never started");
    await newQuestion("complete after reload");
    await sampleUntil("Done.", 45000);
    const meta = await evalJS(byId("rp-meta") + ".textContent");
    assert.ok(meta.includes("Question: complete after reload") && !meta.includes("reload one"), meta);
  });

  await scenario("stale poll cannot overwrite a newer run", "REAL BROWSER; MOCKED provider; takeover SIMULATED by advancing the page's internal run counter", async () => {
    await loadPage(false);
    await newQuestion("complete stale");
    await waitFor(byId("rs-status") + ".textContent.startsWith('Research task running')", 10000, "the running state");
    await evalJS("(rpRunId += 1, true)");
    const mark = network.length;
    await sleep(13000);
    const later = await sample();
    assert.ok(later.status.startsWith("Research task running"), "the stale poll changed the page: " + later.status);
    assert.strictEqual(later.hidden, true);
    assert.strictEqual(statusGets(sinceMark(mark)).length, 0, "a stale poll still made a request");
  });

  await scenario("page request log: every logged http(s) request was to the loopback server", "REAL BROWSER; Chrome's request log for the test page, not packet-level proof", async () => {
    const after = network.slice(probeEnd);
    const web = after.filter((r) => /^https?:/i.test(r.url));
    const other = after.filter((r) => !/^https?:/i.test(r.url));
    // Anything that is not http(s) (for example a data: or blob: URL) never leaves the browser; list it so it is visible.
    const kinds = {};
    other.forEach((r) => { const k = r.url.slice(0, 40).replace(/\s+/g, " "); kinds[k] = (kinds[k] || 0) + 1; });
    say("       non-http(s) requests seen: " + JSON.stringify(kinds));
    const hosts = new Set(web.map((r) => new URL(r.url).host));
    assert.deepStrictEqual([...hosts], ["127.0.0.1:" + sitePort], JSON.stringify([...hosts]));
    assert.ok(web.length > 30, "suspiciously few requests were logged");
  });

  await scenario("no script errors or console errors in the page", "REAL BROWSER", async () => {
    const real = consoleProblems.filter((p) => !(p.url || "").endsWith("/favicon.ico") && !/favicon/i.test(p.text));
    assert.deepStrictEqual(real, [], JSON.stringify(real));
  });

  const benign = consoleProblems.filter((p) => !real_problem(p));
  if (benign.length) say("note: " + benign.length + " console entry(ies) about /favicon.ico (the demo serves none); not a failure");
}

function real_problem(p) { return !((p.url || "").endsWith("/favicon.ico") || /favicon/i.test(p.text)); }

// ------------------------------------------------------------------ cleanup
async function shutdown() {
  try { if (cdp) await Promise.race([cdp.send("Browser.close"), sleep(3000)]); } catch (e) { /* closing */ }
  if (cdp) cdp.close();
  for (let i = 0; i < 100 && chrome && chromeExit === null; i++) await sleep(100);
  if (chrome && chromeExit === null) chrome.kill();       // our own child only
  if (server) {
    try { server.stdin.end(); } catch (e) { /* closed */ }
    for (let i = 0; i < 50 && server.exitCode === null; i++) await sleep(100);
    if (server.exitCode === null) server.kill();          // our own child only
  }
  await sleep(1000);
}

// Looks for credential-like text in everything retained. It stores only file names and the pattern that matched.
function scanEvidenceFiles() {
  const files = fs.readdirSync(EVID).sort();
  const hits = [];
  for (const name of files) {
    const text = fs.readFileSync(path.join(EVID, name), "utf8");
    for (const p of SECRET_PATTERNS) if (p.re.test(text)) hits.push({ file: name, pattern: p.label });
  }
  return { files, patterns_checked: SECRET_PATTERNS.map((p) => p.label), hits };
}

let finishing = false;
async function finish(fatal) {
  if (finishing) return;
  finishing = true;
  await shutdown();
  persist("INCOMPLETE");                       // make sure the files exist before the final scan
  evidenceScan = scanEvidenceFiles();
  const state = outcome(fatal);
  say({ PASSED: "ALL " + results.length + " SCENARIOS PASSED", FAILED: "FAILED: " + results.filter((r) => !r.pass).length +
    " scenario(s) failed" + (evidenceScan.hits.length ? " and the evidence scan found credential-like text" : ""),
    INCOMPLETE: "INCOMPLETE: " + results.length + " of " + EXPECTED_SCENARIOS + " scenarios ran" + (fatal ? " and the run aborted" : "") }[state] +
    " (provider calls logged: " + providerCalls().length + "; evidence scan hits: " + evidenceScan.hits.length + ")");
  say("evidence kept in " + path.relative(ROOT, EVID) + ": " + evidenceScan.files.join(", ") + " + STATUS.txt = " + state);
  persist(state);
  if (state === "PASSED") {
    // Only the throwaway Chrome profile folder is removed, and only after a fully passing run. Evidence is never removed.
    const entries = fs.existsSync(TMP) ? fs.readdirSync(TMP).sort() : [];
    if (entries.every(isOwnEntry)) {
      let removed = false;
      for (let i = 0; i < 8 && !removed; i++) {
        try { fs.rmSync(TMP, { recursive: true, force: true }); removed = !fs.existsSync(TMP); } catch (e) { await sleep(1000); }
      }
      console.log(removed ? "cleanup: .browser_tmp (Chrome profile) removed" : "cleanup: .browser_tmp could not be fully removed and was left in place");
    } else {
      console.log("cleanup: unexpected entries in .browser_tmp (" + entries.join(", ") + "); left in place");
    }
  } else {
    console.log("cleanup: .browser_tmp left in place for inspection");
  }
  process.exit({ PASSED: 0, FAILED: 1, INCOMPLETE: 2 }[state]);
}

process.on("uncaughtException", (e) => { say("ABORT - uncaught: " + (e && e.message ? e.message : e)); finish(e); });
process.on("unhandledRejection", (e) => { say("ABORT - unhandled: " + (e && e.message ? e.message : e)); finish(e); });

(async () => {
  let fatal = null;
  try { await main(); } catch (e) { fatal = e; say("ABORT - " + (e && e.message ? e.message : e)); }
  await finish(fatal);
})();
