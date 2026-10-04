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
const summarySource = source.slice(source.indexOf("function summary(d)"), source.indexOf("async function syncDocument(documentId)"));
const ageTarget = { scope: "extended", section: "pregnancy", field: "maternal_age_years" };
let opened;
const summaryContext = vm.createContext({
  state: { decisions: {} },
  el: (tag, attrs, ...children) => ({ tag, attrs, children, append(...items) { this.children.push(...items); } }),
  button: (label, click) => { const control = { label, click }; controls.push(control); return control; },
  $: () => ({ value: "reviewer" }), basename: name => name,
  v1Entries: () => [],
  extendedTargets: () => [{ target: ageTarget, fv: { value: 29, verification: { state: "UNVERIFIED" } } }],
  plural: (count, label) => `${count} ${label}`, agree: (count, label) => label,
  openField: target => { opened = target; }, confirmDocument: () => {},
});
vm.runInContext(summarySource, summaryContext);
controls = [];
const summaryCard = summaryContext.summary({ review: { selection: { choice: "NEW" }, selection_warnings: [], encounter_matches: [] }, pages: [], draft: {} });
assert.match(JSON.stringify(summaryCard), /Aucune visite/);
assert.match(JSON.stringify(summaryCard), /export Excel/);
controls.find(control => control.label === "Vérifier les données Excel").click();
assert.equal(opened, ageTarget);
process.stdout.write("UI controls and export review guidance passed\n");
