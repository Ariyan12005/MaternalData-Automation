"use strict";

// Exercise the rendered controls and their callbacks without a live server.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("dayone/static/app.js", "utf8");
const renderSource = source.slice(source.indexOf("function renderConversationalCard(p)"),
  source.indexOf("async function replyConversational(payload)"));
let controls = [], reply, scrolled = 0, focused = 0;
const context = vm.createContext({
  state: {},
  el: (tag, attrs, ...children) => ({ tag, attrs, children }),
  button: (label, click) => { const control = { label, click }; controls.push(control); return control; },
  replyConversational: payload => { reply = JSON.parse(JSON.stringify(payload)); },
  $: () => ({ scrollIntoView: () => scrolled++, focus: () => focused++ }),
});
vm.runInContext(renderSource, context);
context.renderConversationalCard({ step: "PATIENT", expected_revision: 7,
  candidates: [{ patient_id: "PAT-000001" }], actions: ["CHOOSE", "NEW", "UNSURE"] });
controls.find(control => control.label.includes("PAT-000001")).click();
assert.deepEqual(reply, { action: "CHOOSE", patient_id: "PAT-000001", expected_revision: 7 });
controls.find(control => control.label.includes("sûre")).click();
assert.deepEqual(reply, { action: "UNSURE", expected_revision: 7 });
controls = [];
context.renderConversationalCard({ step: "EXISTING_VISITS", expected_revision: 8, actions: ["UPDATE", "KEEP"] });
assert.equal(controls.length, 1);
controls[0].click();
assert.equal(scrolled, 1);
assert.equal(focused, 1);
controls = [];
context.renderConversationalCard({ step: "FIELD", field: { scope: "page" }, actions: ["SECTION"] });
controls[0].click();
assert.equal(scrolled, 2);
assert.equal(focused, 2);
controls = [];
context.renderConversationalCard({ step: "CONFIRM", expected_revision: 9, actions: ["CONFIRM"] });
controls[0].click();
assert.deepEqual(reply, { action: "CONFIRM", expected_revision: 9 });
process.stdout.write("UI controls: patient choice, uncertainty, existing visits, section review and revision passed\n");
