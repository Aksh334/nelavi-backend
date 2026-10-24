"use strict";
// Runs the REAL demo_app/static/app.js (minus its final start() call) against a minimal fake page.
// No browser, no network, no real timers: fetch and setTimeout are scripted. Used by tests/test_research_pro.py.
// Usage: node tests/js_harness_research_pro.js <path to app.js>

const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const source = fs.readFileSync(process.argv[2], "utf8").replace(/\nstart\(\);\s*$/, "\n");
assert.ok(!/\nstart\(\);\s*$/.test(source), "the final start() call was not removed");

class Element {
  constructor() {
    this.children = [];
    this._text = "";
    this.hidden = false;
    this.disabled = false;
    this.className = "";
    this.value = "";
    this.dataset = {};
    this.listeners = {};
  }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get firstChild() { return this.children[0] || null; }
  appendChild(c) { this.children.push(c); return c; }
  removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); return c; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  set innerHTML(v) { throw new Error("innerHTML must never be used"); }
}

function textOf(el) { return el._text + el.children.map(textOf).join(""); }
function reply(status, body) { return { ok: status >= 200 && status < 300, status, json: async () => body }; }

function makePage(handler) {
  const elements = {};
  const created = [];
  const document = {
    getElementById(id) { return elements[id] || (elements[id] = new Element()); },
    createElement() { const e = new Element(); created.push(e); return e; },
    createTextNode(t) { const e = new Element(); e._text = String(t); return e; },
  };
  const timers = [];
  const clock = { now: 1000000 };
  const fetchCalls = [];
  const context = {
    document, console, URL,
    Date: { now: () => clock.now },
    fetch: async (url, options) => { fetchCalls.push({ url, options: options || {} }); return handler(url, options || {}); },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
  };
  vm.createContext(context);
  vm.runInContext(source, context, { filename: "app.js" });
  const page = {
    el: (id) => document.getElementById(id), created, timers, clock, fetchCalls,
    run: (code) => vm.runInContext(code, context),
    async drain(max) {
      let n = 0;
      while (timers.length && n++ < (max || 100)) {
        const t = timers.shift();
        clock.now += t.ms;
        await t.fn();
      }
      assert.ok(n < (max || 100), "timers did not stop: polling is not bounded");
    },
  };
  return page;
}

const CONFIG = { configured: true, domain: "icar.org.in", model: "pro", query_max_chars: 200, deadline_seconds: 120 };
const TASK = "task_0123456789abcdef";

function view(overrides) {
  return Object.assign({
    kind: "research_pro_unreviewed", task_id: TASK, status: "running", error_code: null, query: "q",
    elapsed_seconds: 0, deadline_seconds: 120, retry_after_seconds: null, retrieved_at: null, label: "ignored",
    content: null, sources: [], sources_dropped: 0, sources_state: "not_available",
    caveats: [{ code: "c", message: "Caveat text." }],
  }, overrides);
}

// A server whose start reply and successive status replies are scripted.
function server(startReply, statusReplies) {
  const queue = statusReplies.slice();
  return (url) => {
    if (url.startsWith("/api/research-pro/start")) return startReply();
    if (url.startsWith("/api/research-pro/status")) {
      const next = queue.length > 1 ? queue.shift() : queue[0];
      return typeof next === "function" ? next() : reply(200, next);
    }
    throw new Error("unexpected request: " + url);
  };
}

function ready(handler, question) {
  const page = makePage(handler);
  page.run("configureResearchPro(" + JSON.stringify(CONFIG) + ")");
  page.el("rs-question").value = question === undefined ? "How is soil sampled?" : question;
  return page;
}

const scenarios = {
  async "configuration writes the warning wording and enables the button only when the server confirms it"() {
    const page = makePage(() => { throw new Error("no request expected"); });
    page.run("configureResearchPro({ configured: false })");
    assert.strictEqual(page.run("researchProConfig"), null);
    page.run("configureResearchPro(null)");
    page.run("configureResearchPro({ configured: true })");
    assert.strictEqual(page.run("researchProConfig"), null);
    assert.strictEqual(page.el("rs-submit").disabled, false, "untouched when not configured");
    page.run("configureResearchPro(" + JSON.stringify(CONFIG) + ")");
    const notice = page.el("rs-notice").textContent;
    for (const expected of ["REAL SERVICE", "icar.org.in", "120 seconds", "15 to 250 credits", "unreviewed"]) {
      assert.ok(notice.includes(expected), expected);
    }
    assert.ok(page.el("rs-help").textContent.includes("not sent"));
    assert.strictEqual(page.el("rs-submit").disabled, false);
    assert.strictEqual(page.el("rs-question").maxLength, 200);
  },

  async "a completed task shows the label, the text as text and only approved links"() {
    const sources = [
      { title: "Good", url: "https://www.icar.org.in/a" },
      { title: "Evil", url: "https://evil.example/x" },
      { title: "Script", url: "javascript:alert(1)" },
      { title: "Look-alike", url: "https://icar.org.in.evil.com/" },
      { title: "Credentials", url: "https://icar.org.in@evil.example/" },
      { title: "Plain http", url: "http://icar.org.in/" },
      { title: "<b>markup</b>", url: "https://icar.org.in/b" },
    ];
    const page = ready(server(() => reply(200, view()), [
      view({ status: "running", elapsed_seconds: 5 }),
      view({ status: "completed", content: "Generated <b>text</b>\nline two", sources, sources_dropped: 2,
        sources_state: "approved", retrieved_at: "2026-01-02T03:04:05+00:00" }),
    ]));
    await page.run("submitResearchPro(null)");
    const start = page.fetchCalls[0];
    assert.strictEqual(start.url, "/api/research-pro/start");
    assert.strictEqual(start.options.method, "POST");
    assert.strictEqual(start.options.headers["X-Nelavi-Demo"], "1");
    assert.deepStrictEqual(JSON.parse(start.options.body), { question: "How is soil sampled?" });
    assert.strictEqual(page.run("rsBusy"), true);
    assert.strictEqual(page.el("rs-submit").disabled, true);
    assert.strictEqual(page.el("rp-result").hidden, true, "nothing is shown while the task runs");
    assert.ok(page.el("rs-status").textContent.startsWith("Research task running"));
    await page.drain();
    assert.ok(page.el("rs-status").textContent.startsWith("Done."), page.el("rs-status").textContent);
    assert.ok(page.el("rs-status").className.includes("ok"));
    assert.strictEqual(page.el("rp-result").hidden, false);
    const label = page.el("rp-label").textContent;
    assert.ok(label.includes("UNREVIEWED AI-GENERATED RESEARCH") && label.includes("NOT VERIFIED AGRICULTURAL ADVICE"));
    assert.strictEqual(page.el("rp-text").textContent, "Generated <b>text</b>\nline two");
    const items = page.el("rp-sources").children;
    assert.deepStrictEqual(items.map((li) => li.children[0].href),
      ["https://www.icar.org.in/a", "https://icar.org.in/b"]);
    assert.strictEqual(items[1].children[0].textContent, "<b>markup</b>");
    for (const li of items) {
      assert.ok(li.children[0].rel.includes("noopener") && li.children[0].rel.includes("noreferrer"));
    }
    assert.strictEqual(page.created.filter((e) => e.href !== undefined).length, 2, "no other element got a link");
    assert.ok(page.el("rp-sources-note").textContent.includes("2 returned source(s) were not on the approved domain"));
    assert.ok(!textOf(page.el("rp-result")).includes("evil.example"));
    assert.strictEqual(page.run("rsBusy"), false);
    assert.strictEqual(page.el("rs-submit").disabled, false);
    assert.strictEqual(page.fetchCalls.length, 3);
  },

  async "a completed task with no approved source says so and shows no link"() {
    const page = ready(server(() => reply(200, view()), [view({
      status: "completed", content: "Some text", sources: [{ title: "Evil", url: "https://evil.example/" }],
      sources_dropped: 1, sources_state: "none_approved" })]));
    await page.run("submitResearchPro(null)");
    await page.drain();
    assert.strictEqual(page.el("rp-sources").children.length, 0);
    assert.ok(page.el("rp-sources-note").textContent.startsWith("No approved sources available"));
    assert.strictEqual(page.el("rp-text").textContent, "Some text");
    assert.ok(page.el("rp-label").textContent.includes("UNREVIEWED"));
  },

  async "failed, timed out and errored tasks show a clear state and no text or links"() {
    const cases = [
      [view({ status: "failed", error_code: "task_failed" }), "reported that the task failed"],
      [view({ status: "timed_out", error_code: "deadline_exceeded" }), "did not finish within 120 seconds"],
      [view({ status: "error", error_code: "provider_unavailable" }), "server-side problem"],
      [view({ status: "error", error_code: "something_new" }), "The research service reported an error."],
    ];
    for (const [terminal, expected] of cases) {
      const page = ready(server(() => reply(200, view()), [terminal]));
      await page.run("submitResearchPro(null)");
      await page.drain();
      const text = page.el("rs-status").textContent;
      assert.ok(text.includes(expected), text);
      assert.ok(text.includes("No result was produced."), text);
      assert.ok(page.el("rs-status").className.includes("bad"));
      assert.strictEqual(page.el("rp-result").hidden, true);
      assert.strictEqual(page.run("rsBusy"), false);
      assert.strictEqual(page.el("rs-submit").disabled, false);
    }
  },

  async "a refused start shows fixed wording with the code and nothing else"() {
    const page = ready(server(() => reply(422, view({ status: "error", error_code: "rate_limited", task_id: null })), [view()]));
    await page.run("submitResearchPro(null)");
    const text = page.el("rs-status").textContent;
    assert.ok(text.startsWith("Error: ") && text.includes("rate limiting") && text.includes("(code: rate_limited)"), text);
    assert.strictEqual(page.timers.length, 0);
    assert.strictEqual(page.run("rsBusy"), false);
    assert.strictEqual(page.el("rp-result").hidden, true);
    assert.strictEqual(page.el("rs-submit").disabled, false);
  },

  async "a server error message is shown as text and a malformed start reply is not followed"() {
    let page = ready(server(() => reply(400, { error: "Send JSON like {\"question\": \"<text>\"}." }), [view()]));
    await page.run("submitResearchPro(null)");
    assert.ok(page.el("rs-status").textContent.startsWith("Error: Send JSON like"));
    for (const body of [{ foo: 1 }, view({ status: "completed" }), view({ task_id: 5 }), view({ kind: "other" }), null]) {
      page = ready(server(() => reply(200, body), [view()]));
      await page.run("submitResearchPro(null)");
      assert.ok(page.el("rs-status").textContent.includes("was not understood"), JSON.stringify(body));
      assert.strictEqual(page.timers.length, 0);
      assert.strictEqual(page.run("rsBusy"), false);
    }
  },

  async "a second click while a task runs is ignored"() {
    const page = ready(server(() => reply(200, view()), [view()]));
    const first = page.run("submitResearchPro(null)");
    const second = page.run("submitResearchPro(null)");
    await Promise.all([first, second]);
    assert.strictEqual(page.fetchCalls.filter((c) => c.url === "/api/research-pro/start").length, 1);
    await page.run("submitAdvisory(null)");  // the form's handler is the same guarded path
    assert.strictEqual(page.fetchCalls.filter((c) => c.url === "/api/research-pro/start").length, 1);
    assert.ok(!page.fetchCalls.some((c) => c.url === "/api/advisory"), "the Search route must not be used");
  },

  async "the form handler starts Research Pro, not Search, when Research Pro is configured"() {
    const page = ready(server(() => reply(200, view()), [view({ status: "failed", error_code: "task_failed" })]));
    await page.run("submitAdvisory(null)");
    assert.strictEqual(page.fetchCalls[0].url, "/api/research-pro/start");
    assert.ok(!page.fetchCalls.some((c) => c.url === "/api/advisory"));
  },

  async "input is checked before anything is sent"() {
    let page = ready(server(() => { throw new Error("must not be called"); }, [view()]), "   ");
    await page.run("submitResearchPro(null)");
    assert.ok(page.el("rs-status").textContent.includes("The question is required."));
    page = ready(server(() => { throw new Error("must not be called"); }, [view()]), "x".repeat(201));
    await page.run("submitResearchPro(null)");
    assert.ok(page.el("rs-status").textContent.includes("longer than 200 characters"));
    assert.strictEqual(page.fetchCalls.length, 0);
    assert.strictEqual(page.run("rsBusy"), false);
    page = makePage(() => { throw new Error("must not be called"); });
    page.el("rs-question").value = "q";
    await page.run("submitResearchPro(null)");  // not configured: does nothing at all
    assert.strictEqual(page.fetchCalls.length, 0);
  },

  async "polling honours retry-after, stops on a server error, and gives up after repeated network failures"() {
    let page = ready(server(() => reply(200, view()), [
      view({ status: "running", retry_after_seconds: 30 }),
      view({ status: "completed", content: "Text", sources: [], sources_state: "none_approved" })]));
    await page.run("submitResearchPro(null)");
    assert.strictEqual(page.timers[0].ms, 5000);
    await page.timers.shift().fn();
    assert.strictEqual(page.timers[0].ms, 30000, "the wait asked for by the service is respected");
    assert.ok(page.el("rs-status").textContent.includes("asked us to wait 30 seconds"));
    await page.drain();
    assert.ok(page.el("rs-status").textContent.startsWith("Done."));

    page = ready(server(() => reply(200, view()), [() => reply(404, { error: "That research task is unknown or has expired." })]));
    await page.run("submitResearchPro(null)");
    await page.drain();
    assert.strictEqual(page.el("rs-status").textContent, "Error: That research task is unknown or has expired.");
    assert.strictEqual(page.run("rsBusy"), false);
    assert.strictEqual(page.fetchCalls.length, 2);

    page = ready(server(() => reply(200, view()), [() => { throw new Error("offline"); }]));
    await page.run("submitResearchPro(null)");
    await page.drain();
    assert.ok(page.el("rs-status").textContent.includes("could not read the task state"));
    assert.strictEqual(page.fetchCalls.length, 4, "one start and three failed polls, then it stops");
    assert.strictEqual(page.run("rsBusy"), false);

    page = ready(server(() => reply(200, view()), [{ foo: 1 }]));
    await page.run("submitResearchPro(null)");
    await page.drain();
    assert.ok(page.el("rs-status").textContent.includes("could not read the task state"));
    assert.strictEqual(page.el("rp-result").hidden, true);
  },

  async "the page stops waiting shortly after the server's own deadline"() {
    const page = ready(server(() => reply(200, view()), [view({ status: "running" })]));
    await page.run("submitResearchPro(null)");
    await page.drain(200);
    assert.ok(page.el("rs-status").textContent.includes("The page stopped waiting"));
    assert.ok(page.fetchCalls.length <= 30, "polling stayed bounded: " + page.fetchCalls.length);
    assert.strictEqual(page.run("rsBusy"), false);
    assert.strictEqual(page.el("rp-result").hidden, true);
  },

  async "a new task replaces the old one and a stale poll cannot overwrite it"() {
    const page = ready(server(() => reply(200, view()), [view({ status: "running" })]));
    await page.run("submitResearchPro(null)");
    const stale = page.timers.shift();      // the old task's pending poll
    page.run("rsBusy = false");              // simulate the old run having been released
    page.run("rpRunId += 1");                // a newer run now owns the page
    const before = page.fetchCalls.length;
    await stale.fn();
    assert.strictEqual(page.fetchCalls.length, before, "a stale poll makes no request");
  },
};

(async () => {
  let failed = 0;
  for (const [name, run] of Object.entries(scenarios)) {
    try {
      await run();
      console.log("ok   - " + name);
    } catch (e) {
      failed += 1;
      console.log("FAIL - " + name + "\n       " + (e && e.stack ? e.stack : e));
    }
  }
  if (failed) {
    console.log(failed + " scenario(s) failed");
    process.exit(1);
  }
  console.log("all scenarios passed (" + Object.keys(scenarios).length + ")");
})();
