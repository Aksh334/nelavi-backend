"use strict";
// Runs the REAL demo_app/static/app.js (minus its final start() call) against a minimal fake page and renders one
// integrated-run response with the real renderIntegrated(). No browser, no network, no timers.
// Used by tests/test_demo_nvidia_diagnostics.py.
// Usage: node tests/js_harness_integrated_result.js <path to app.js> <path to response.json>
// Prints one JSON line: {"rendered": bool, "text": "<all text in #iw-body>", "hidden": bool}

const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(process.argv[2], "utf8").replace(/\nstart\(\);\s*$/, "\n");
if (/\nstart\(\);\s*$/.test(source)) throw new Error("the final start() call was not removed");
const data = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

class Element {
  constructor() {
    this.children = [];
    this._text = "";
    this.hidden = false;
    this.className = "";
    this.value = "";
    this.dataset = {};
  }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get firstChild() { return this.children[0] || null; }
  appendChild(c) { this.children.push(c); return c; }
  removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); return c; }
  addEventListener() {}
  set innerHTML(v) { throw new Error("innerHTML must never be used"); }
}

function textOf(el) { return el._text + el.children.map(textOf).join(" "); }

const elements = {};
const document = {
  getElementById(id) { return elements[id] || (elements[id] = new Element()); },
  createElement() { return new Element(); },
  createTextNode(t) { const e = new Element(); e._text = String(t); return e; },
};
const context = { document, console, URL, __data: data };
vm.createContext(context);
vm.runInContext(source, context, { filename: "app.js" });
const rendered = vm.runInContext("renderIntegrated(__data)", context);
const body = document.getElementById("iw-body");
console.log(JSON.stringify({ rendered, text: textOf(body), hidden: document.getElementById("iw-result").hidden }));
