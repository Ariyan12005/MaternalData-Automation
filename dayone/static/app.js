"use strict";

const REVIEWABLE = new Set(["AI_PROCESSED", "NEEDS_REVIEW", "VALIDATED", "PATIENT_MATCHED", "DUPLICATE_SUSPECTED"]);
const SPECIMEN_PATIENT_ONE = /\/dossiers_specimen_10_patientes-0[1-8](?:__[^/]*)?\.png$/;

const DOC_STATUS = {
  CAPTURED: "Réception des pages", PENDING_AI: "En attente IA", AI_PROCESSED: "Traité par l'IA",
  NEEDS_REVIEW: "À vérifier", VALIDATED: "Champs validés", PATIENT_MATCHED: "Patiente choisie",
  REGISTERED: "Enregistré", PROCESSING_FAILED: "Échec extraction", DUPLICATE_SUSPECTED: "Doublon suspecté",
  MANUAL_REVIEW_REQUIRED: "Photo à reprendre",
};
const FIELD_STATUS = {
  KNOWN: "lu", NEEDS_REVIEW: "à vérifier", ILLEGIBLE: "illisible", NOT_PROVIDED: "non renseigné",
  UNKNOWN: "inconnu", NOT_APPLICABLE: "non applicable",
};
const VERIFICATION = { UNVERIFIED: "", CONFIRMED: "confirmé", CORRECTED: "corrigé" };
const OUTCOME = {
  NEW: "nouvelle visite", EXISTS_SAME: "déjà enregistrée (identique)",
  EXISTS_DIFFERENT: "déjà enregistrée, valeurs différentes", MISSING_DATE: "date manquante",
  CREATED: "créée", UNCHANGED: "déjà enregistrée, inchangée", UPDATED: "mise à jour",
  KEPT_EXISTING: "existante conservée",
};
const REASON = {
  NEEDS_REVIEW: "Je ne suis pas sûre de cette valeur.",
  ILLEGIBLE: "Je n'arrive pas à lire ce qui est écrit.",
  REQUIRED_MISSING: "Ce champ est obligatoire pour enregistrer la visite.",
  DUPLICATE_ENCOUNTER_DATE: "Deux visites de ce dossier ont la même date.",
};
const FLAG = {
  DIGIT_UNCLEAR: "chiffre peu lisible", MARKED_NOT_DONE: "noté « NF »", CROSSED_OUT: "case barrée",
  SECTION_NOT_CAPTURED: "page non photographiée", MANUAL_ENTRY: "saisie manuelle",
  KEY_MISMATCH: "une clé diffère", MULTIPLE_CANDIDATES: "plusieurs correspondances",
  NO_KEY_MATCH: "aucune clé de la fiche ne correspond", SLOT_DIFFERS: "colonne différente",
};
const HINT = {
  date: "jj/mm/aaaa", gestational_age: "ex. 28SA+1j", decimal: "ex. 63", int: "ex. 27",
  bp: "ex. 12 ou 120", enum: "négatif ou positif", digits: "chiffres", code: "ex. 164125", text: "",
};

const state = {
  system: null,
  senderId: null,
  senderLabel: "",
  media: [],
  showAllMedia: false,
  selectedMedia: [],
  captures: [],
  cameraStream: null,
  lastMessage: null,
  documentId: null,
  detail: null,
  edit: null,
  decisions: {},
  changingPatient: false,
  patientId: null,
  error: null,
  busy: false,
  signatures: {},
};

const $ = (selector) => document.querySelector(selector);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function button(label, onclick, cls = "", { type = "button", disabled = false } = {}) {
  return el("button", { type, class: cls, onclick, disabled: disabled || state.busy }, label);
}

async function api(method, path, body) {
  const response = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-Reviewer": $("#reviewer").value.trim() },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error?.message || `Erreur ${response.status}`);
    Object.assign(error, { code: data.error?.code, details: data.error?.details, status: response.status });
    throw error;
  }
  return data;
}

// ---------------------------------------------------------------- formatting

const spec = (name) => state.system.catalog.fields[name];
const slotLabel = (slot) => state.system.catalog.slots[slot] || slot;
const basename = (ref) => ref.split("/").pop();
const mediaUrl = (ref) => "/media/" + encodeURIComponent(ref);
const pct = (c) => (c === null || c === undefined ? "—" : `${Math.round(c * 100)} %`);

function fmtDate(iso) {
  if (!iso) return "—";
  const [y, m, d] = iso.slice(0, 10).split("-");
  return `${d}/${m}/${y}`;
}

function fmtTime(iso) {
  return new Date(iso).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
}

function fmtValue(name, value) {
  if (value === null || value === undefined) return "—";
  const s = spec(name);
  if (s.kind === "date") return fmtDate(value);
  if (s.kind === "gestational_age") return `${Math.floor(value / 7)} SA + ${value % 7} j`;
  if (s.kind === "enum") return value === "NEGATIVE" ? "négatif" : "positif";
  return s.unit ? `${value} ${s.unit}` : String(value);
}

function fmtField(name, fv) {
  return fv.value !== null ? fmtValue(name, fv.value) : FIELD_STATUS[fv.field_status];
}

function fmtBp(fields) {
  const sys = fields.systolic_bp_mmhg?.value;
  const dia = fields.diastolic_bp_mmhg?.value;
  return sys == null && dia == null ? "—" : `${sys ?? "?"}/${dia ?? "?"}`;
}

function getField(draft, target) {
  return target.scope === "document"
    ? draft.document_fields[target.field]
    : draft.encounters[target.encounter_index].fields[target.field];
}

const sameTarget = (a, b) =>
  a && b && a.scope === b.scope && a.field === b.field && (a.encounter_index ?? null) === (b.encounter_index ?? null);

function chip(status) {
  return el("span", { class: `chip s-${status}` }, DOC_STATUS[status] || status);
}

function notify(text, kind = "info") {
  const toast = $("#toast");
  toast.textContent = text;
  toast.className = `toast ${kind}`;
  toast.hidden = false;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => { toast.hidden = true; }, 5000);
}

// ---------------------------------------------------------------- actions

async function act(fn) {
  if (state.busy) return;
  state.busy = true;
  state.error = null;
  try {
    await fn();
    state.edit = null;
  } catch (error) {
    state.error = error.message;
  } finally {
    state.busy = false;
    await refreshAll(true);
  }
}

function revision() {
  return state.detail?.document.revision;
}

function reviewField(target, action, extra = {}) {
  return act(() => api("POST", `/api/documents/${state.documentId}/fields`, {
    ...target, action, ...extra, expected_revision: revision(),
  }));
}

function selectPatient(choice, patientId = null) {
  return act(async () => {
    await api("POST", `/api/documents/${state.documentId}/patient`, {
      choice, patient_id: patientId, expected_revision: revision(),
    });
    state.changingPatient = false;
  });
}

function confirmDocument() {
  return act(async () => {
    const result = await api("POST", `/api/documents/${state.documentId}/confirm`, {
      existing_visit_decisions: state.decisions, expected_revision: revision(),
    });
    state.patientId = result.patient_id;
    state.decisions = {};
  });
}

function startManualEntry() {
  return act(() => api("POST", `/api/documents/${state.documentId}/manual-entry`, {}));
}

function requestRetake(page) {
  const reason = window.prompt(`Motif pour la sage-femme (page ${page.position}) :`, "photo floue ou coupée");
  if (reason === null) return;
  act(() => api("POST", `/api/pages/${page.page_id}/retake`, { reason }));
}

function movePage(pageId, target) {
  return act(() => api("POST", `/api/pages/${pageId}/move`, { target_document_id: target }));
}

function selectDocument(documentId) {
  if (documentId === state.documentId) return;
  Object.assign(state, { documentId, edit: null, decisions: {}, changingPatient: false, error: null });
  state.signatures.detail = null;
  refreshDetail(true);
  renderQueue(state.queue || []);
}

function newMessageId() {
  const random = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `wamid.sim-${random}`;
}

async function sendPhotos() {
  const refs = [...state.selectedMedia];
  state.selectedMedia = [];
  renderMedia();
  for (const ref of refs) {
    const body = { sender_id: state.senderId, message_id: newMessageId(), media_ref: ref };
    try {
      const result = await api("POST", "/api/whatsapp/messages", body);
      state.lastMessage = body;
      if (result.document_id !== state.documentId) selectDocument(result.document_id);
    } catch (error) {
      notify(error.message, "error");
      break;
    }
  }
  $("#replay-last").disabled = !state.lastMessage;
  await refreshAll(true);
}

async function replayLastMessage() {
  try {
    const result = await api("POST", "/api/whatsapp/messages", state.lastMessage);
    notify(result.duplicate
      ? `Webhook rejoué (${state.lastMessage.message_id.slice(0, 22)}…) : doublon ignoré, pas de nouvel accusé.`
      : "Message traité.");
  } catch (error) {
    notify(error.message, "error");
  }
  await refreshAll(true);
}

// ---------------------------------------------------------------- camera

async function uploadPhoto(blob) {
  const response = await fetch("/api/media", {
    method: "POST",
    headers: { "Content-Type": blob.type || "image/jpeg", "X-Reviewer": $("#reviewer").value.trim() },
    body: blob,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error?.message || `Erreur ${response.status}`);
  state.captures = [...state.captures.filter((ref) => ref !== data.media_ref), data.media_ref];
  if (!state.selectedMedia.includes(data.media_ref)) state.selectedMedia = [...state.selectedMedia, data.media_ref];
  renderMedia();
  notify("Photo prête : appuyez sur Envoyer.");
}

async function openCamera() {
  if (!navigator.mediaDevices?.getUserMedia) {
    $("#camera-file").click();  // phones open the camera app, computers a file picker
    return;
  }
  try {
    state.cameraStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "environment", width: { ideal: 1920 }, height: { ideal: 2560 } }, audio: false,
    });
  } catch (error) {
    notify("Caméra indisponible : choisissez un fichier.", "error");
    $("#camera-file").click();
    return;
  }
  $("#camera-video").srcObject = state.cameraStream;
  $("#camera-dialog").showModal();
}

function closeCamera() {
  state.cameraStream?.getTracks().forEach((track) => track.stop());
  state.cameraStream = null;
  if ($("#camera-dialog").open) $("#camera-dialog").close();
}

async function shootPhoto() {
  const video = $("#camera-video");
  const canvas = document.createElement("canvas");
  canvas.width = video.videoWidth;
  canvas.height = video.videoHeight;
  canvas.getContext("2d").drawImage(video, 0, 0);
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.92));
  closeCamera();
  try {
    await uploadPhoto(blob);
  } catch (error) {
    notify(error.message, "error");
  }
}

// ---------------------------------------------------------------- refresh

async function refreshAll(force = false) {
  try {
    await Promise.all([refreshSystem(), refreshThread(force), refreshQueue(force)]);
    await refreshDetail(force);
    await refreshTimeline(force);
  } catch (error) {
    console.error(error);
  }
}

async function refreshSystem() {
  const system = await api("GET", "/api/system");
  state.system = system;
  $("#ai-toggle").checked = system.ai_available;
}

async function refreshThread(force) {
  if (!state.senderId) return;
  const messages = await api("GET", `/api/whatsapp/thread?sender_id=${encodeURIComponent(state.senderId)}`);
  const signature = `${messages.length}|${messages.at(-1)?.id}`;
  if (!force && signature === state.signatures.thread) return;
  state.signatures.thread = signature;
  renderThread(messages);
}

async function refreshQueue(force) {
  const documents = await api("GET", "/api/documents");
  const signature = JSON.stringify(documents.map((d) => [d.document_id, d.status, d.blocking_count, d.page_count]));
  state.queue = documents;
  if (!force && signature === state.signatures.queue) return;
  state.signatures.queue = signature;
  renderQueue(documents);
}

async function refreshDetail(force) {
  if (!state.documentId) {
    state.detail = null;
    renderReview();
    return;
  }
  if (state.edit && !force) return;
  let detail;
  try {
    detail = await api("GET", `/api/documents/${state.documentId}`);
  } catch (error) {
    if (error.status === 404) {
      Object.assign(state, { documentId: null, detail: null });
      renderReview();
    }
    return;
  }
  const signature = [detail.document.status, detail.document.revision, detail.events.length,
    state.system.ai_available, state.busy].join("|");
  if (!force && signature === state.signatures.detail) return;
  state.signatures.detail = signature;
  state.detail = detail;
  renderReview();
}

async function refreshTimeline(force) {
  const patients = await api("GET", "/api/patients");
  if (!state.patientId && patients.length) state.patientId = patients[0].patient_id;
  const timeline = state.patientId ? await api("GET", `/api/patients/${state.patientId}/timeline`) : null;
  const signature = JSON.stringify([patients.map((p) => [p.patient_id, p.visit_count]), state.patientId,
    timeline?.visits.map((v) => [v.visit_id, v.updated_at]), state.documentId]);
  if (!force && signature === state.signatures.timeline) return;
  state.signatures.timeline = signature;
  renderTimeline(patients, timeline);
}

// ---------------------------------------------------------------- phone

function renderThread(messages) {
  const box = $("#thread");
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
  box.replaceChildren(...messages.map((m) => el("div", { class: `bubble ${m.direction === "IN" ? "mine" : "theirs"}` },
    m.media_ref ? el("img", { src: mediaUrl(m.media_ref), alt: basename(m.media_ref) }) : null,
    m.media_ref ? el("small", {}, basename(m.media_ref)) : null,
    m.body ? el("span", {}, m.body) : null,
    el("time", {}, fmtTime(m.created_at)),
  )));
  if (atBottom || messages.length < 4) box.scrollTop = box.scrollHeight;
}

function renderMedia() {
  const dataset = state.showAllMedia ? state.media : state.media.filter((ref) => SPECIMEN_PATIENT_ONE.test(ref));
  const list = [...state.captures, ...dataset];
  $("#media-grid").replaceChildren(...list.map((ref) => {
    const order = state.selectedMedia.indexOf(ref);
    const capture = state.captures.includes(ref);
    return el("button", {
      type: "button",
      class: `thumb ${order >= 0 ? "selected" : ""} ${capture ? "capture" : ""}`,
      title: basename(ref),
      onclick: () => {
        state.selectedMedia = order >= 0 ? state.selectedMedia.filter((r) => r !== ref) : [...state.selectedMedia, ref];
        renderMedia();
      },
    },
    el("img", { src: mediaUrl(ref), alt: basename(ref), loading: "lazy" }),
    el("span", {}, capture ? `photo ${state.captures.indexOf(ref) + 1}` : basename(ref)),
    order >= 0 ? el("b", { class: "order" }, order + 1) : null);
  }));
  const send = $("#send-photos");
  send.disabled = state.selectedMedia.length === 0;
  send.textContent = state.selectedMedia.length ? `Envoyer (${state.selectedMedia.length})` : "Envoyer";
}

// ---------------------------------------------------------------- queue

function renderQueue(documents) {
  const queue = $("#queue");
  if (!documents.length) {
    queue.replaceChildren(el("li", { class: "empty" }, "Aucun dossier."));
    return;
  }
  queue.replaceChildren(...documents.map((doc) => {
    const details = [`${doc.page_count} page(s)`];
    if (doc.encounter_count) details.push(`${doc.encounter_count} visite(s)`);
    if (doc.blocking_count) details.push(`${doc.blocking_count} à vérifier`);
    if (doc.patient_id) details.push(doc.patient_id);
    return el("li", {
      class: doc.document_id === state.documentId ? "active" : "",
      onclick: () => selectDocument(doc.document_id),
    },
    el("div", { class: "row" }, el("strong", {}, doc.document_id), chip(doc.status)),
    el("div", { class: "muted" }, details.join(" · ")));
  }));
}

// ---------------------------------------------------------------- review

function renderReview() {
  const root = $("#review");
  const d = state.detail;
  if (!d) {
    root.replaceChildren(el("p", { class: "empty" },
      "Sélectionnez un dossier, ou envoyez les pages spécimen 01, 02 et 03 depuis le téléphone."));
    return;
  }
  root.replaceChildren(...[
    renderDocumentHeader(d),
    renderPages(d),
    renderConversation(d),
    d.draft ? renderFieldGrid(d) : null,
  ].filter(Boolean));
}

function renderDocumentHeader(d) {
  const doc = d.document;
  const extraction = d.draft?.extraction;
  return el("div", { class: "doc-header" },
    el("h2", {}, doc.document_id), chip(doc.status),
    el("span", { class: `chip grouping-${doc.grouping_status}` },
      doc.grouping_status === "PROVISIONAL" ? "Regroupement provisoire" : "Regroupement confirmé"),
    el("span", { class: "muted" }, `rév. ${doc.revision}`),
    extraction ? el("span", { class: "muted" }, `extraction : ${extraction.extractor} ${extraction.extractor_version}`) : null);
}

function renderPages(d) {
  const locked = d.document.status === "REGISTERED";
  return el("div", { class: "pages" }, d.pages.map((page) => el("figure", { class: "page" },
    el("a", { href: mediaUrl(page.media_ref), target: "_blank", rel: "noopener" },
      el("img", { src: mediaUrl(page.media_ref), alt: basename(page.media_ref) })),
    el("figcaption", {}, `p.${page.position} · ${basename(page.media_ref)}`),
    page.retake_requested_at ? el("span", { class: "retake" }, "nouvelle photo demandée") : null,
    locked || page.retake_requested_at || d.document.status === "CAPTURED" ? null
      : button("Reprendre la photo", () => requestRetake(page), "small"),
    locked ? null : el("select", {
      "aria-label": `Déplacer la page ${page.position}`,
      disabled: state.busy,
      onchange: (event) => {
        const value = event.target.value;
        if (!value) return;
        if (!window.confirm("Déplacer cette page relance l'extraction des dossiers concernés et annule leurs vérifications. Continuer ?")) {
          event.target.value = "";
          return;
        }
        movePage(page.page_id, value === "__new" ? null : value);
      },
    },
    el("option", { value: "" }, "Déplacer…"),
    d.pages.length > 1 ? el("option", { value: "__new" }, "→ nouveau dossier (séparer)") : null,
    d.move_targets.map((target) => el("option", { value: target }, `→ ${target}`))))));
}

function renderConversation(d) {
  const chat = el("div", { class: "chat" });
  const doc = d.document;
  chat.append(botMessage(`Dossier ${doc.document_id} : ${d.pages.length} page(s) reçue(s) par WhatsApp.`));

  if (d.draft) {
    const encounters = d.draft.encounters;
    const pii = d.draft.pii_detected.length;
    chat.append(botMessage(
      `Extraction : ${encounters.length} visite(s) détectée(s) (${encounters.map((e) => slotLabel(e.slot)).join(", ")}).`
      + (pii ? ` ${pii} donnée(s) personnelle(s) vue(s) sur la fiche, non extraite(s).` : "")));
  }

  for (const event of d.events) {
    const node = historyMessage(event, d);
    if (node) chat.append(node);
  }

  const current = currentStep(d);
  if (current) chat.append(current);
  if (state.error) chat.append(el("div", { class: "msg error" }, state.error));
  return chat;
}

function botMessage(text, extra = null) {
  return el("div", { class: "msg bot" }, el("p", {}, text), extra);
}

function agentMessage(actor, text) {
  return el("div", { class: "msg agent" }, el("small", {}, actor), el("p", {}, text));
}

function historyMessage(event, d) {
  const detail = event.detail;
  if (event.type === "FIELD_REVIEWED") {
    const label = spec(detail.field).label + (detail.slot ? ` (${slotLabel(detail.slot)})` : "");
    const value = detail.new.value !== null ? fmtValue(detail.field, detail.new.value) : FIELD_STATUS[detail.new.field_status];
    const verb = { CONFIRM: "Confirmé", CORRECT: "Corrigé", SET_STATUS: "Statut" }[detail.action];
    return agentMessage(event.actor, `${verb} : ${label} → ${value}`);
  }
  if (event.type === "PATIENT_SELECTED") {
    const text = { EXISTING: `Patiente : ${detail.patient_id}`, NEW: "Nouvelle patiente", UNSURE: "Je ne sais pas" }[detail.choice];
    return agentMessage(event.actor, text);
  }
  if (event.type === "PAGES_REGROUPED") {
    return el("div", { class: "msg note" },
      `Pages regroupées par ${event.actor} (${detail.page_id} : ${detail.from} → ${detail.to}). `
      + `${detail.discarded_reviews} vérification(s) annulée(s), nouvelle extraction.`);
  }
  if (event.type === "RETAKE_REQUESTED") {
    return el("div", { class: "msg note" }, `Nouvelle photo de la page ${detail.position} demandée par ${event.actor}`
      + (detail.reason ? ` : « ${detail.reason} »` : "") + ".");
  }
  if (event.type === "PAGE_RETAKEN") {
    return el("div", { class: "msg note" }, `Nouvelle photo reçue pour la page ${detail.position}.`);
  }
  if (event.type === "MANUAL_ENTRY_STARTED") {
    return el("div", { class: "msg note" }, `Saisie manuelle démarrée par ${event.actor}.`);
  }
  return null;
}

function currentStep(d) {
  const step = d.next_step;
  if (state.edit && REVIEWABLE.has(d.document.status)) {
    const pending = d.review.blocking.find((item) => sameTarget(state.edit, item));
    return fieldQuestion(d, pending || { ...state.edit, reason: null }, d.review.blocking.length);
  }
  if (step === "COLLECTING") {
    return botMessage(`Réception des pages en cours. Le dossier part en extraction ${state.system.grouping_window_seconds} s après la dernière photo.`);
  }
  if (step === "WAITING_AI") {
    return botMessage(state.system.ai_available
      ? "En file d'attente pour l'extraction…"
      : "IA indisponible : le dossier attend dans la file. Rien n'est perdu ; il sera traité au retour de l'IA.");
  }
  if (step === "WAITING_RETAKE") {
    const pages = d.pages.filter((p) => p.retake_requested_at).map((p) => p.position).join(", ");
    return botMessage(`En attente d'une nouvelle photo de la page ${pages} : le message a été envoyé à la sage-femme. `
      + "L'extraction reprendra automatiquement à la réception.");
  }
  if (step === "FAILED") {
    return el("div", { class: "msg bot question" },
      el("p", {}, `Je n'ai pas pu extraire ce groupe de pages (${d.document.failure_reason}). `
        + "Séparez ou regroupez les pages si elles concernent plusieurs femmes, ou saisissez les données à la main."),
      el("div", { class: "actions" }, button("Saisie manuelle", startManualEntry, "primary")));
  }
  if (step === "FIELD") {
    const blocking = d.review.blocking;
    return fieldQuestion(d, blocking[0], blocking.length);
  }
  if (step === "PATIENT" || (state.changingPatient && (step === "CONFIRM" || step === "EXISTING_VISITS"))) {
    return patientQuestion(d);
  }
  if (step === "CONFIRM" || step === "EXISTING_VISITS") return summary(d);
  if (step === "DONE") return registrationMessage(d);
  return null;
}

function fieldQuestion(d, item, remaining = 0) {
  const target = { scope: item.scope, encounter_index: item.encounter_index ?? null, field: item.field };
  const fv = getField(d.draft, target);
  const s = spec(item.field);
  const encounter = target.scope === "encounter" ? d.draft.encounters[target.encounter_index] : null;
  const title = s.label + (encounter ? ` — ${slotLabel(encounter.slot)}` : "");

  let read = "Aucune valeur lue.";
  if (fv.value !== null) read = `J'ai lu « ${fv.raw_text ?? "—"} », soit ${fmtValue(item.field, fv.value)}.`;
  else if (fv.raw_text) read = `J'ai vu « ${fv.raw_text} » mais je ne peux pas le lire.`;
  const confidence = fv.confidence !== null ? ` Confiance : ${pct(fv.confidence)}.` : "";
  const flags = fv.validation_flags.length ? ` (${fv.validation_flags.map((f) => FLAG[f] || f).join(", ")})` : "";
  const reason = item.reason ? REASON[item.reason] : "Modification demandée depuis la grille.";
  const editing = sameTarget(state.edit, target);

  return el("div", { class: "msg bot question" },
    el("div", { class: "q-title" }, title,
      remaining ? el("span", { class: "muted" }, ` · ${remaining} point(s) à vérifier`) : null),
    el("p", {}, `${read}${confidence} ${reason}${flags}`),
    sourceThumb(fv.source),
    editing ? correctionForm(target) : el("div", { class: "actions" },
      fv.value !== null ? button("Confirmer", () => reviewField(target, "CONFIRM"), "primary") : null,
      button("Corriger", () => { state.edit = target; state.error = null; renderReview(); }),
      s.required ? null : fv.field_status === "ILLEGIBLE"
        ? button("Confirmer « illisible »", () => reviewField(target, "CONFIRM"))
        : button("Illisible", () => reviewField(target, "SET_STATUS", { field_status: "ILLEGIBLE" })),
      s.required ? null : button("Non renseigné", () => reviewField(target, "SET_STATUS", { field_status: "NOT_PROVIDED" })),
      item.reason ? null : button("Fermer", () => { state.edit = null; renderReview(); }, "ghost")));
}

function sourceThumb(source) {
  if (!source) return null;
  const where = [source.row, source.column].filter(Boolean).join(" / ");
  return el("a", { class: "source", href: mediaUrl(source.page_ref), target: "_blank", rel: "noopener" },
    el("img", { src: mediaUrl(source.page_ref), alt: "" }),
    el("span", {}, `${basename(source.page_ref)} — ${where}`));
}

function correctionForm(target) {
  const s = spec(target.field);
  const input = el("input", { type: "text", placeholder: HINT[s.kind] || "", "aria-label": `Nouvelle valeur : ${s.label}` });
  setTimeout(() => input.focus(), 0);
  return el("form", {
    class: "correction",
    onsubmit: (event) => {
      event.preventDefault();
      reviewField(target, "CORRECT", { value: input.value });
    },
  },
  input,
  button("Valider", null, "primary", { type: "submit" }),
  button("Annuler", () => { state.edit = null; state.error = null; renderReview(); }, "ghost"));
}

function patientQuestion(d) {
  const review = d.review;
  const candidates = review.candidates;
  const suggested = review.suggested_patient_id;
  const node = el("div", { class: "msg bot question" }, el("div", { class: "q-title" }, "Quelle patiente ?"));
  if (review.selection?.choice === "UNSURE") {
    node.append(el("p", { class: "warn" },
      "Dossier mis de côté (doublon suspecté) : rien ne sera enregistré tant qu'une décision n'est pas prise."));
  }
  node.append(el("p", {}, candidates.length
    ? "Correspondance(s) trouvée(s) dans cet établissement avec les clés lues sur la fiche :"
    : "Aucune patiente de cet établissement ne porte ces clés."));
  candidates.forEach((candidate, index) => node.append(candidateCard(candidate, index, candidate.patient_id === suggested)));
  node.append(el("div", { class: "actions" },
    candidates.map((candidate, index) => button(`Patiente ${index + 1} : ${candidate.patient_id}`,
      () => selectPatient("EXISTING", candidate.patient_id), candidate.patient_id === suggested ? "primary" : "")),
    button("Nouvelle patiente", () => selectPatient("NEW")),
    button("Je ne sais pas", () => selectPatient("UNSURE"), "ghost"),
    state.changingPatient ? button("Annuler", () => { state.changingPatient = false; renderReview(); }, "ghost") : null));
  if (suggested) {
    node.append(el("p", { class: "hint" },
      "Proposition automatique à confirmer : rien n'est enregistré sans votre validation finale."));
  }
  return node;
}

function candidateCard(candidate, index, suggested) {
  const reasons = [
    ...candidate.matched_keys.map((key) => `${spec(key).label} identique`),
    ...candidate.conflicting_keys.map((key) => `${spec(key).label} différent (${candidate.keys[key]})`),
  ];
  const visits = `${candidate.visit_count} visite(s)`
    + (candidate.last_visit_date ? `, dernière le ${fmtDate(candidate.last_visit_date)}` : "");
  return el("div", { class: `candidate ${suggested ? "suggested" : ""}` },
    el("div", {}, el("strong", {}, `Patiente ${index + 1} : ${candidate.patient_id}`),
      suggested ? el("span", { class: "badge" }, "proposée") : null),
    el("div", {}, reasons.join(" · ")),
    el("div", { class: "muted" }, visits),
    candidate.flags.length ? el("div", { class: "warn" }, candidate.flags.map((f) => FLAG[f] || f).join(", ")) : null);
}

function summary(d) {
  const review = d.review;
  const selection = review.selection;
  const reviewer = $("#reviewer").value.trim();
  const node = el("div", { class: "msg bot question summary" },
    el("div", { class: "q-title" }, "Récapitulatif avant enregistrement"));

  node.append(el("p", {},
    selection.choice === "EXISTING"
      ? `Patiente : ${selection.patient_id} (choisie par ${selection.by}). `
      : "Nouvelle patiente : l'identifiant sera créé à l'enregistrement. ",
    button("Changer", () => { state.changingPatient = true; renderReview(); }, "link")));
  if (review.selection_warnings.length) {
    node.append(el("p", { class: "warn" },
      `Attention : ${review.selection_warnings.map((f) => FLAG[f] || f).join(", ")}.`));
  }
  node.append(el("p", { class: "muted" },
    `Pages : ${d.pages.map((p) => basename(p.media_ref)).join(", ")} (le regroupement sera confirmé).`));

  const rows = review.encounter_matches.map((match) => {
    const fields = d.draft.encounters[match.encounter_index].fields;
    const index = match.encounter_index;
    let decision = null;
    if (match.outcome === "EXISTS_DIFFERENT") {
      const diffs = match.diffs.map((diff) =>
        `${spec(diff.field).label} ${fmtValue(diff.field, diff.existing)} → ${fmtValue(diff.field, diff.draft)}`);
      decision = el("div", { class: "decision" }, el("small", {}, diffs.join(" ; ")),
        ["UPDATE", "KEEP"].map((choice) => button(choice === "UPDATE" ? "Mettre à jour" : "Garder l'existante",
          () => { state.decisions[index] = choice; renderReview(); },
          state.decisions[index] === choice ? "primary small" : "small")));
    }
    return el("tr", {},
      el("td", {}, slotLabel(match.slot)),
      el("td", {}, fmtDate(match.visit_date)),
      el("td", { class: `outcome o-${match.outcome}` }, OUTCOME[match.outcome]),
      el("td", {}, fmtField("gestational_age_days", fields.gestational_age_days)),
      el("td", {}, fmtField("weight_kg", fields.weight_kg)),
      el("td", {}, fmtBp(fields)),
      el("td", {}, fmtField("fundal_height_cm", fields.fundal_height_cm)),
      el("td", {}, decision));
  });
  node.append(el("table", { class: "summary-table" },
    el("thead", {}, el("tr", {}, ["Visite", "Date", "Résultat", "AG", "Poids", "TA", "HU", ""].map((h) => el("th", {}, h)))),
    el("tbody", {}, rows)));

  const undecided = review.encounter_matches.filter((m) => m.outcome === "EXISTS_DIFFERENT" && !state.decisions[m.encounter_index]);
  node.append(el("p", { class: "muted" },
    `Les champs non modifiés seront marqués « confirmés » par ${reviewer || "l'agent"}.`));
  node.append(el("div", { class: "actions" },
    button("CONFIRMER l'enregistrement", confirmDocument, "primary big", { disabled: undecided.length > 0 })));
  return node;
}

function registrationMessage(d) {
  const registration = d.registration;
  return el("div", { class: "msg bot question done" },
    el("div", { class: "q-title" }, "Enregistré"),
    el("p", {}, `Par ${registration.registered_by} à ${fmtTime(registration.registered_at)}, `
      + `patiente ${registration.patient_id}${registration.patient_created ? " (créée)" : ""}.`),
    el("ul", {}, registration.visits.map((visit) => el("li", {},
      `${visit.visit_id} — ${fmtDate(visit.visit_date)} (${slotLabel(visit.slot)}) : ${OUTCOME[visit.outcome]}`))),
    el("div", { class: "actions" }, button("Voir le suivi de la patiente", () => {
      state.patientId = registration.patient_id;
      refreshTimeline(true);
      $(".timeline-panel").scrollIntoView({ block: "start" });
    }, "primary")));
}

function renderFieldGrid(d) {
  const draft = d.draft;
  const catalog = state.system.catalog;
  const editable = REVIEWABLE.has(d.document.status);

  const cell = (target, fv) => {
    const verification = VERIFICATION[fv.verification.state];
    return el("td", {
      class: `cell st-${fv.field_status} v-${fv.verification.state} ${sameTarget(state.edit, target) ? "editing" : ""}`,
      title: `${FIELD_STATUS[fv.field_status]} · confiance ${pct(fv.confidence)}${verification ? ` · ${verification}` : ""}`
        + (fv.raw_text ? ` · lu « ${fv.raw_text} »` : ""),
      onclick: editable ? () => { state.edit = target; state.error = null; renderReview(); } : null,
    },
    el("div", { class: "val" }, fmtField(target.field, fv)),
    el("div", { class: "meta" }, `${pct(fv.confidence)}${verification ? ` · ${verification}` : ""}`));
  };

  const header = el("table", { class: "grid" }, el("tbody", {}, catalog.document_fields.map((name) =>
    el("tr", {}, el("th", {}, spec(name).label),
      cell({ scope: "document", encounter_index: null, field: name }, draft.document_fields[name])))));

  const grid = el("table", { class: "grid" },
    el("thead", {}, el("tr", {}, el("th", {}, "Champ"), draft.encounters.map((e) => el("th", {}, slotLabel(e.slot))))),
    el("tbody", {}, catalog.encounter_fields.map((name) => el("tr", {},
      el("th", {}, spec(name).label),
      draft.encounters.map((encounter, index) =>
        cell({ scope: "encounter", encounter_index: index, field: name }, encounter.fields[name]))))));

  return el("details", { class: "field-grid", open: true },
    el("summary", {}, editable ? "Tous les champs (cliquer une case pour la modifier)" : "Tous les champs"),
    el("div", { class: "legend" },
      ["KNOWN", "NEEDS_REVIEW", "ILLEGIBLE", "NOT_PROVIDED"].map((s) => el("span", { class: `cell st-${s}` }, FIELD_STATUS[s])),
      el("span", { class: "cell v-CORRECTED" }, "corrigé"), el("span", { class: "cell v-CONFIRMED" }, "confirmé")),
    el("div", { class: "grids" }, header, grid));
}

// ---------------------------------------------------------------- timeline

function renderTimeline(patients, timeline) {
  const select = $("#patient-select");
  select.replaceChildren(...patients.map((p) =>
    el("option", { value: p.patient_id, selected: p.patient_id === state.patientId },
      `${p.patient_id} · ${p.visit_count} visite(s)`)));
  const box = $("#timeline");
  if (!timeline) {
    box.replaceChildren(el("p", { class: "empty" }, "Aucune patiente enregistrée."));
    return;
  }
  const p = timeline.patient;
  const keys = [`N° fiche ${p.registry_file_number ?? "—"}`, `code ${p.midwife_patient_code ?? "—"}`];
  box.replaceChildren(
    el("p", { class: "muted" }, `${p.patient_id} — ${p.facility_name} · ${keys.join(" · ")} · ${timeline.visits.length} visite(s)`
      + (p.flags.length ? ` · ${p.flags.join(", ")}` : "")),
    el("table", { class: "timeline" },
      el("thead", {}, el("tr", {}, ["Date", "Visite", "AG", "Poids", "TA", "HU", "Syphilis", "VIH", "Sources"].map((h) => el("th", {}, h)))),
      el("tbody", {}, timeline.visits.map((visit) => {
        const f = visit.fields;
        return el("tr", { class: visit.source_documents.includes(state.documentId) ? "fresh" : "" },
          el("td", {}, fmtDate(visit.visit_date)),
          el("td", {}, slotLabel(visit.slot)),
          el("td", {}, fmtField("gestational_age_days", f.gestational_age_days)),
          el("td", {}, fmtField("weight_kg", f.weight_kg)),
          el("td", {}, fmtBp(f)),
          el("td", {}, fmtField("fundal_height_cm", f.fundal_height_cm)),
          el("td", {}, fmtField("syphilis_test", f.syphilis_test)),
          el("td", {}, fmtField("hiv_test", f.hiv_test)),
          el("td", { class: "muted" }, visit.source_documents.join(", ") + (visit.revisions ? ` · ${visit.revisions} mise(s) à jour` : "")));
      }))));
}

// ---------------------------------------------------------------- init

async function init() {
  const reviewerInput = $("#reviewer");
  reviewerInput.value = localStorage.getItem("dayone.reviewer") || reviewerInput.value;
  reviewerInput.addEventListener("change", () => localStorage.setItem("dayone.reviewer", reviewerInput.value.trim()));

  await refreshSystem();
  const senders = await api("GET", "/api/senders");
  state.senderId = senders[0]?.sender_id ?? null;
  $("#sender-label").textContent = senders[0]?.label ?? "";
  state.media = await api("GET", "/api/media");

  $("#ai-toggle").addEventListener("change", async (event) => {
    await api("POST", "/api/system/ai", { available: event.target.checked });
    notify(event.target.checked ? "IA disponible : la file d'attente est traitée." : "IA indisponible : les dossiers restent en file d'attente.");
    await refreshAll(true);
  });
  $("#show-all-media").addEventListener("change", (event) => { state.showAllMedia = event.target.checked; renderMedia(); });
  $("#send-photos").addEventListener("click", sendPhotos);
  $("#open-camera").addEventListener("click", openCamera);
  $("#camera-shoot").addEventListener("click", shootPhoto);
  $("#camera-close").addEventListener("click", closeCamera);
  $("#camera-dialog").addEventListener("close", closeCamera);
  $("#camera-file").addEventListener("change", async (event) => {
    const file = event.target.files[0];
    event.target.value = "";
    if (file) await uploadPhoto(file).catch((error) => notify(error.message, "error"));
  });
  $("#replay-last").addEventListener("click", replayLastMessage);
  $("#patient-select").addEventListener("change", (event) => { state.patientId = event.target.value; refreshTimeline(true); });
  $("#reset-demo").addEventListener("click", async () => {
    if (!window.confirm("Effacer toutes les données et recharger la visite antérieure de démonstration ?")) return;
    await api("POST", "/api/demo/reset", {});
    Object.assign(state, { documentId: null, detail: null, edit: null, decisions: {}, patientId: null, lastMessage: null, error: null });
    $("#replay-last").disabled = true;
    await refreshAll(true);
  });

  renderMedia();
  await refreshAll(true);
  setInterval(() => refreshAll(false), 1500);
}

init().catch((error) => notify(`Impossible de démarrer : ${error.message}`, "error"));
