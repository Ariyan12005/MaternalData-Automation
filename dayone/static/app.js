"use strict";

const REVIEWABLE = new Set(["AI_PROCESSED", "NEEDS_REVIEW", "VALIDATED", "PATIENT_MATCHED", "DUPLICATE_SUSPECTED"]);

const DOC_STATUS = {
  CAPTURED: "Réception des pages", PENDING_AI: "En attente IA", AI_PROCESSED: "Traité par l'IA",
  NEEDS_REVIEW: "À vérifier", VALIDATED: "Champs validés", PATIENT_MATCHED: "Patiente choisie",
  REGISTERED: "Enregistré", PROCESSING_FAILED: "Échec extraction", DUPLICATE_SUSPECTED: "Doublon suspecté",
};
const FIELD_STATUS = {
  KNOWN: "lu", NEEDS_REVIEW: "à vérifier", ILLEGIBLE: "illisible", NOT_PROVIDED: "non renseigné",
  UNKNOWN: "inconnu", NOT_APPLICABLE: "non applicable",
};
const VERIFICATION = { UNVERIFIED: "", CONFIRMED: "confirmé", CORRECTED: "corrigé" };
const RETAKE_STATUS = { PENDING: "En attente de la photo", FULFILLED: "Nouvelle photo reçue", CANCELLED: "Annulée" };
const OUTCOME = {
  NEW: "nouvelle visite", EXISTS_SAME: "déjà enregistrée (identique)",
  EXISTS_DIFFERENT: "déjà enregistrée, valeurs différentes", MISSING_DATE: "date manquante",
  CREATED: "créée", UNCHANGED: "déjà enregistrée, inchangée", UPDATED: "mise à jour",
  KEPT_EXISTING: "existante conservée",
};
const REASON = {
  NEEDS_REVIEW: "L'IA n'est pas sûre de cette valeur.",
  ILLEGIBLE: "L'IA n'arrive pas à lire ce qui est écrit.",
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
  lastMessage: null,
  thread: [],
  reply: null,
  queue: null,
  documentId: null,
  detail: null,
  edit: null,
  decisions: {},
  changingPatient: false,
  patientId: null,
  error: null,
  busy: false,
  connectionLost: false,
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

function button(label, onclick, cls = "", { type = "button", disabled = false, key = null, ariaLabel = null } = {}) {
  return el("button", {
    type, class: cls, onclick, disabled: disabled || state.busy, "aria-label": ariaLabel,
    "data-key": key ?? (typeof label === "string" ? label : null),
  }, label);
}

/** Re-renders a region without losing keyboard focus: the focused control is found again by its data-key. */
function replaceKeepingFocus(root, nodes, fallbackSelector = null) {
  const active = document.activeElement;
  const inside = active && active !== root && root.contains(active);
  const key = inside ? active.dataset.key : null;
  root.replaceChildren(...nodes.filter(Boolean));
  if (!inside) return;
  const target = (key && root.querySelector(`[data-key="${CSS.escape(key)}"]`))
    || (fallbackSelector && root.querySelector(fallbackSelector));
  target?.focus();
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
const plural = (n, word) => `${n} ${word}${n > 1 ? "s" : ""}`;
const agree = (n, word) => `${word}${n > 1 ? "s" : ""}`;

function confidenceText(c) {
  if (c === null || c === undefined) return "confiance : sans objet";
  const level = c >= 0.75 ? "élevée" : c >= 0.5 ? "moyenne" : "faible";
  return `confiance ${level} (${Math.round(c * 100)} %)`;
}

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
const targetKey = (t) => `cell-${t.scope}-${t.encounter_index ?? "doc"}-${t.field}`;

function chip(status) {
  return el("span", { class: `chip s-${status}` }, DOC_STATUS[status] || status);
}

function notify(text, kind = "info") {
  const toast = $("#toast");
  toast.textContent = text;
  toast.className = `toast ${kind}`;
  toast.hidden = false;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => { toast.hidden = true; }, 6000);
}

// ---------------------------------------------------------------- actions

async function act(fn) {
  if (state.busy) return;
  state.busy = true;
  state.error = null;
  $("#review").setAttribute("aria-busy", "true");
  try {
    await fn();
    state.edit = null;
  } catch (error) {
    state.error = error.message;
  } finally {
    state.busy = false;
    $("#review").removeAttribute("aria-busy");
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
    notify(`Enregistré : ${plural(result.visits.length, "visite")} pour ${result.patient_id}.`, "success");
  });
}

function startManualEntry() {
  return act(() => api("POST", `/api/documents/${state.documentId}/manual-entry`, {}));
}

function movePage(pageId, target) {
  return act(() => api("POST", `/api/pages/${pageId}/move`, { target_document_id: target }));
}

function requestRetake(page) {
  const text = `Merci de reprendre la photo de la page ${page.position}.`;
  if (!window.confirm(`Envoyer ce message à la sage-femme ?\n\n« ${text} »\n\n`
    + "La confirmation du dossier sera bloquée jusqu'à la réception de la nouvelle photo.")) return;
  return act(async () => {
    const result = await api("POST", `/api/pages/${page.page_id}/retake`, {});
    notify(result.created
      ? `Demande de reprise envoyée pour la page ${result.position}.`
      : `Une demande est déjà en attente pour la page ${result.position} : aucun nouveau message envoyé.`, "success");
  });
}

function cancelRetake(requestId) {
  if (!window.confirm("Annuler la demande de reprise ? La sage-femme sera prévenue qu'elle n'a rien à renvoyer.")) return;
  return act(async () => {
    await api("POST", `/api/retakes/${requestId}/cancel`, {});
    notify("Demande de reprise annulée.", "success");
  });
}

function selectDocument(documentId) {
  if (documentId === state.documentId) return;
  Object.assign(state, { documentId, detail: null, edit: null, decisions: {}, changingPatient: false, error: null });
  state.signatures.detail = null;
  renderReview();
  refreshDetail(true);
  renderQueue(state.queue || []);
}

function newMessageId() {
  const random = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `wamid.sim-${random}`;
}

async function sendPhotos() {
  const refs = [...state.selectedMedia];
  const reply = state.reply;
  state.selectedMedia = [];
  renderComposer();
  for (const ref of refs) {
    const body = { sender_id: state.senderId, message_id: newMessageId(), media_ref: ref };
    if (reply) body.retake_request_id = reply.requestId;
    try {
      const result = await api("POST", "/api/whatsapp/messages", body);
      state.lastMessage = body;
      if (reply) {
        state.reply = null;
        notify(`Nouvelle photo associée à la demande ${reply.requestId} : l'extraction va être relancée.`, "success");
      }
      if (result.document_id !== state.documentId) selectDocument(result.document_id);
    } catch (error) {
      notify(error.message, "error");
      break;
    }
  }
  $("#replay-last").disabled = !state.lastMessage;
  renderComposer();
  await refreshAll(true);
}

async function replayLastMessage() {
  try {
    const result = await api("POST", "/api/whatsapp/messages", state.lastMessage);
    notify(result.duplicate
      ? `Webhook rejoué (${state.lastMessage.message_id.slice(0, 22)}…) : doublon ignoré, aucun nouvel accusé.`
      : "Message traité.");
  } catch (error) {
    notify(error.message, "error");
  }
  await refreshAll(true);
}

function startReply(message) {
  state.reply = { requestId: message.retake_request_id, body: message.body };
  state.selectedMedia = [];
  renderThread(state.thread);
  renderComposer();
  $("#media-grid button")?.focus();
}

function cancelReply() {
  state.reply = null;
  state.selectedMedia = [];
  renderThread(state.thread);
  renderComposer();
}

// ---------------------------------------------------------------- refresh

async function refreshAll(force = false) {
  try {
    await Promise.all([refreshSystem(), refreshThread(force), refreshQueue(force)]);
    await refreshDetail(force);
    await refreshTimeline(force);
    if (state.connectionLost) {
      state.connectionLost = false;
      notify("Connexion au serveur rétablie.", "success");
    }
  } catch (error) {
    console.error(error);
    if (!state.connectionLost && error instanceof TypeError) {
      state.connectionLost = true;
      notify("Serveur injoignable : nouvel essai automatique toutes les 1,5 s.", "error");
    }
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
  state.thread = messages;
  if (state.reply && !messages.some((m) => m.retake_request_id === state.reply.requestId && m.retake_status === "PENDING")) {
    state.reply = null;
    renderComposer();
  }
  const signature = JSON.stringify([messages.length, messages.at(-1)?.id, messages.map((m) => m.retake_status ?? ""),
    state.reply?.requestId]);
  if (!force && signature === state.signatures.thread) return;
  state.signatures.thread = signature;
  renderThread(messages);
}

async function refreshQueue(force) {
  const documents = await api("GET", "/api/documents");
  const signature = JSON.stringify(documents.map((d) =>
    [d.document_id, d.status, d.blocking_count, d.page_count, d.pending_retakes]));
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
      return;
    }
    throw error;
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

// ---------------------------------------------------------------- simulator

function renderThread(messages) {
  const box = $("#thread");
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
  const nodes = messages.length ? messages.map((m) => el("div", { class: `bubble ${m.direction === "IN" ? "mine" : "theirs"}` },
    el("span", { class: "sr-only" }, m.direction === "IN" ? "Sage-femme : " : "Day1 : "),
    m.media_ref ? el("img", { src: mediaUrl(m.media_ref), alt: `Photo envoyée : ${basename(m.media_ref)}` }) : null,
    m.media_ref ? el("small", {}, basename(m.media_ref)) : null,
    m.body ? el("span", {}, m.body) : null,
    retakeBubbleAction(m),
    el("time", { datetime: m.created_at }, fmtTime(m.created_at)),
  )) : [el("p", { class: "thread-empty" }, "Aucun message. Choisissez des photos ci-dessous puis « Envoyer ».")];
  replaceKeepingFocus(box, nodes);
  if (atBottom || messages.length < 4) box.scrollTop = box.scrollHeight;
}

function retakeBubbleAction(message) {
  if (!message.retake_request_id) return null;
  if (message.retake_status === "PENDING") {
    const active = state.reply?.requestId === message.retake_request_id;
    return el("div", { class: "bubble-action" },
      el("span", { class: "sim-note" }, "Simulateur"),
      button(active ? "Réponse en cours : choisissez la photo" : "Répondre avec une nouvelle photo",
        () => startReply(message), active ? "small primary" : "small",
        { key: `reply-${message.retake_request_id}`, disabled: active }));
  }
  return el("small", { class: `retake-state rs-${message.retake_status}` },
    message.retake_status === "FULFILLED" ? "Reprise : nouvelle photo envoyée" : "Reprise : demande annulée");
}

function renderComposer() {
  const banner = $("#reply-banner");
  if (state.reply) {
    banner.hidden = false;
    banner.replaceChildren(
      el("div", {}, el("strong", {}, "Réponse à : "), `« ${state.reply.body} »`),
      el("div", { class: "muted" }, "Choisissez une seule photo : elle remplacera la page demandée."),
      button("Annuler la réponse", cancelReply, "small ghost"));
  } else {
    banner.hidden = true;
    banner.replaceChildren();
  }
  $("#composer-hint").textContent = state.reply
    ? "Nouvelle photo pour la page demandée"
    : "Choisissez les photos de la fiche, dans l'ordre";
  renderMedia();
}

function renderMedia() {
  const list = state.showAllMedia ? state.media : state.media.filter((ref) => /\/1-\d+\.jpg$/.test(ref));
  replaceKeepingFocus($("#media-grid"), list.map((ref) => {
    const order = state.selectedMedia.indexOf(ref);
    return el("button", {
      type: "button",
      class: `thumb ${order >= 0 ? "selected" : ""}`,
      "aria-pressed": order >= 0 ? "true" : "false",
      "aria-label": basename(ref) + (order >= 0 ? `, sélectionnée (position ${order + 1})` : ""),
      "data-key": `media-${ref}`,
      onclick: () => {
        if (state.reply) state.selectedMedia = order >= 0 ? [] : [ref];
        else state.selectedMedia = order >= 0 ? state.selectedMedia.filter((r) => r !== ref) : [...state.selectedMedia, ref];
        renderMedia();
      },
    },
    el("img", { src: mediaUrl(ref), alt: "", loading: "lazy" }),
    el("span", {}, basename(ref)),
    order >= 0 ? el("b", { class: "order", "aria-hidden": "true" }, order + 1) : null);
  }));
  const send = $("#send-photos");
  const count = state.selectedMedia.length;
  send.disabled = count === 0;
  send.textContent = state.reply ? "Envoyer la nouvelle photo" : count ? `Envoyer (${count})` : "Envoyer";
}

// ---------------------------------------------------------------- queue

function queueHint(doc) {
  if (doc.pending_retakes && doc.status !== "REGISTERED") return { text: "En attente d'une nouvelle photo", action: false };
  switch (doc.status) {
    case "CAPTURED": return { text: "Réception des pages en cours", action: false };
    case "PENDING_AI": return { text: "En attente d'extraction", action: false };
    case "PROCESSING_FAILED": return { text: "À faire : regrouper, reprendre ou saisir", action: true };
    case "AI_PROCESSED":
    case "NEEDS_REVIEW": return { text: `À faire : ${plural(doc.blocking_count, "champ")} à vérifier`, action: true };
    case "VALIDATED": return { text: "À faire : choisir la patiente", action: true };
    case "DUPLICATE_SUSPECTED": return { text: "À faire : décider (doublon suspecté)", action: true };
    case "PATIENT_MATCHED": return { text: "À faire : confirmer l'enregistrement", action: true };
    case "REGISTERED": return { text: `Enregistré · ${doc.patient_id ?? ""}`, action: false };
    default: return { text: doc.status, action: false };
  }
}

function renderQueue(documents) {
  const queue = $("#queue");
  const todo = documents.filter((doc) => queueHint(doc).action).length;
  $("#queue-count").textContent = documents.length ? `${todo} à traiter` : "";
  if (!documents.length) {
    replaceKeepingFocus(queue, [el("li", { class: "empty" },
      "Aucun dossier. Pour la démo, envoyez 1-1.jpg puis 1-5.jpg depuis le simulateur.")]);
    return;
  }
  replaceKeepingFocus(queue, documents.map((doc) => {
    const hint = queueHint(doc);
    const details = [plural(doc.page_count, "page")];
    if (doc.encounter_count) details.push(plural(doc.encounter_count, "visite"));
    const active = doc.document_id === state.documentId;
    return el("li", {},
      el("button", {
        type: "button",
        class: `queue-item ${active ? "active" : ""} ${hint.action ? "needs-action" : ""}`,
        "aria-current": active ? "true" : null,
        "data-key": `queue-${doc.document_id}`,
        onclick: () => selectDocument(doc.document_id),
      },
      el("span", { class: "row" }, el("strong", {}, doc.document_id), chip(doc.status)),
      el("span", { class: `queue-hint ${hint.action ? "todo" : ""}` }, hint.text),
      el("span", { class: "muted" }, details.join(" · "))));
  }));
}

// ---------------------------------------------------------------- review

function renderReview() {
  const root = $("#review");
  const d = state.detail;
  let nodes;
  if (!state.documentId) {
    const empty = state.queue && state.queue.length === 0;
    nodes = [
      el("h2", { id: "review-title" }, "Aucun dossier sélectionné"),
      el("p", { class: "empty" }, empty
        ? "La file est vide. Pour la démo, envoyez 1-1.jpg puis 1-5.jpg depuis le simulateur de téléphone."
        : "Choisissez un dossier dans la file de vérification pour commencer."),
    ];
  } else if (!d) {
    nodes = [el("h2", { id: "review-title" }, `Dossier ${state.documentId}`),
      el("p", { class: "empty", role: "status" }, "Chargement du dossier…")];
  } else {
    nodes = [renderDocumentHeader(d), renderNextAction(d), renderPages(d), renderRetakes(d),
      d.draft ? renderFieldGrid(d) : null, renderHistory(d)];
  }
  replaceKeepingFocus(root, nodes, "#next-action-title");
}

function renderDocumentHeader(d) {
  const doc = d.document;
  let record = el("span", { class: "draft-badge" }, "Rien d'enregistré pour l'instant");
  if (doc.status === "REGISTERED") record = el("span", { class: "saved-badge" }, "Visites enregistrées");
  else if (d.draft) record = el("span", { class: "draft-badge" }, "Brouillon — non enregistré");
  return el("div", { class: "doc-header" },
    el("h2", { id: "review-title" }, `Dossier ${doc.document_id}`),
    chip(doc.status),
    record,
    el("span", { class: `chip grouping-${doc.grouping_status}` },
      doc.grouping_status === "PROVISIONAL" ? "Regroupement provisoire" : "Regroupement confirmé"),
    el("span", { class: "muted" }, `${plural(d.pages.length, "page")} · révision ${doc.revision}`));
}

function nextActionCopy(d) {
  const blocking = d.review?.blocking.length ?? 0;
  const pendingPages = d.retakes.filter((r) => r.status === "PENDING").map((r) => r.position).join(", ");
  if (state.edit && REVIEWABLE.has(d.document.status)) return ["Modifier un champ", null];
  switch (d.next_step) {
    case "COLLECTING":
      return ["Attendre les autres pages",
        `Les photos arrivent encore. L'extraction démarre ${state.system.grouping_window_seconds} s après la dernière photo.`];
    case "WAITING_AI":
      return state.system.ai_available
        ? ["Extraction en cours", "Le dossier est dans la file de l'IA. Aucune action requise."]
        : ["En attente : IA indisponible", "Rien n'est perdu : le dossier sera traité dès le retour de l'IA."];
    case "WAITING_RETAKE":
      return [`Attendre la nouvelle photo (page ${pendingPages})`,
        "La sage-femme a reçu la demande. La confirmation est bloquée jusqu'à la réception ; "
        + "la nouvelle photo relancera l'extraction et les vérifications."];
    case "FAILED":
      return ["Extraction impossible : choisir une solution",
        `Raison : ${d.document.failure_reason}. Séparez ou regroupez les pages si elles concernent plusieurs femmes, `
        + "demandez une reprise si une photo est illisible, ou saisissez les données à la main."];
    case "FIELD": return [`Vérifier les champs incertains (${blocking} restant${blocking > 1 ? "s" : ""})`, null];
    case "PATIENT": return ["Choisir la patiente", null];
    case "EXISTING_VISITS":
      return ["Décider pour les visites déjà enregistrées",
        "Certaines visites existent déjà avec d'autres valeurs : rien ne sera modifié sans votre choix."];
    case "CONFIRM": return ["Relire et confirmer l'enregistrement", null];
    case "DONE": return ["Terminé : visites enregistrées", null];
    default: return [d.next_step, null];
  }
}

function renderNextAction(d) {
  const [title, text] = nextActionCopy(d);
  return el("section", { class: `next-action step-${d.next_step}`, "aria-labelledby": "next-action-title" },
    el("p", { class: "eyebrow" }, "Prochaine action"),
    el("h3", { id: "next-action-title", tabindex: "-1" }, title),
    text ? el("p", { class: "next-text" }, text) : null,
    currentStep(d),
    state.error ? el("div", { class: "alert", role: "alert" }, el("strong", {}, "Action impossible : "), state.error) : null);
}

function currentStep(d) {
  const step = d.next_step;
  if (state.edit && REVIEWABLE.has(d.document.status)) {
    const pending = d.review.blocking.find((item) => sameTarget(state.edit, item));
    return fieldQuestion(d, pending || { ...state.edit, reason: null }, d.review.blocking.length);
  }
  if (step === "WAITING_RETAKE") {
    return el("div", { class: "actions" }, d.retakes.filter((r) => r.status === "PENDING").map((r) =>
      button(`Annuler la demande (page ${r.position})`, () => cancelRetake(r.request_id), "", { key: `cancel-${r.request_id}` })));
  }
  if (step === "FAILED") {
    return el("div", { class: "actions" }, button("Saisie manuelle", startManualEntry, "primary"));
  }
  if (step === "FIELD") return fieldQuestion(d, d.review.blocking[0], d.review.blocking.length);
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

  let read = "aucune valeur lue";
  if (fv.value !== null) read = `« ${fv.raw_text ?? "—"} », soit ${fmtValue(item.field, fv.value)}`;
  else if (fv.raw_text) read = `« ${fv.raw_text} » (non lisible)`;
  const reason = item.reason ? REASON[item.reason] : "Modification demandée depuis la grille.";
  const editing = sameTarget(state.edit, target);

  return el("div", { class: "action-card" },
    el("div", { class: "q-title" }, title,
      remaining ? el("span", { class: "muted" }, ` · ${plural(remaining, "point")} à vérifier`) : null),
    el("p", { class: "reason" }, reason),
    el("dl", { class: "facts" },
      el("dt", {}, "Lu par l'IA"), el("dd", {}, read),
      el("dt", {}, "Statut"), el("dd", {}, FIELD_STATUS[fv.field_status]),
      el("dt", {}, "Confiance"), el("dd", {}, confidenceText(fv.confidence).replace(/^confiance /, "")),
      fv.validation_flags.length ? [el("dt", {}, "Signalements"),
        el("dd", {}, fv.validation_flags.map((f) => FLAG[f] || f).join(", "))] : null),
    sourceThumb(fv.source),
    editing ? correctionForm(target) : el("div", { class: "actions" },
      fv.value !== null ? button("Confirmer la valeur", () => reviewField(target, "CONFIRM"), "primary") : null,
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
    el("span", {}, `Voir sur la photo : ${basename(source.page_ref)} — ${where}`));
}

function correctionForm(target) {
  const s = spec(target.field);
  const id = `correction-${target.field}`;
  const input = el("input", { id, type: "text", placeholder: HINT[s.kind] || "", autocomplete: "off" });
  setTimeout(() => input.focus(), 0);
  return el("form", {
    class: "correction",
    onsubmit: (event) => {
      event.preventDefault();
      reviewField(target, "CORRECT", { value: input.value });
    },
  },
  el("label", { for: id }, `Nouvelle valeur pour « ${s.label} »` + (HINT[s.kind] ? ` (${HINT[s.kind]})` : "")),
  el("div", { class: "correction-row" },
    input,
    button("Valider la correction", null, "primary", { type: "submit" }),
    button("Annuler", () => { state.edit = null; state.error = null; renderReview(); }, "ghost")));
}

function patientQuestion(d) {
  const review = d.review;
  const candidates = review.candidates;
  const suggested = review.suggested_patient_id;
  const node = el("div", { class: "action-card" });
  if (review.selection?.choice === "UNSURE") {
    node.append(el("p", { class: "warn" },
      "Dossier mis de côté (doublon suspecté) : rien ne sera enregistré tant qu'une décision n'est pas prise."));
  }
  node.append(el("p", {}, candidates.length
    ? "Correspondance(s) trouvée(s) dans cet établissement avec les clés lues sur la fiche :"
    : "Aucune patiente de cet établissement ne porte ces clés."));
  candidates.forEach((candidate, index) => node.append(candidateCard(candidate, index, candidate.patient_id === suggested)));
  node.append(el("div", { class: "actions" },
    candidates.map((candidate, index) => button(`Choisir la patiente ${index + 1} : ${candidate.patient_id}`,
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
  const visits = `${plural(candidate.visit_count, "visite")} ${agree(candidate.visit_count, "enregistrée")}`
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
  const node = el("div", { class: "action-card summary" });

  node.append(el("p", {},
    selection.choice === "EXISTING"
      ? `Patiente : ${selection.patient_id} (choisie par ${selection.by}). `
      : "Nouvelle patiente : l'identifiant sera créé à l'enregistrement. ",
    button("Changer de patiente", () => { state.changingPatient = true; renderReview(); }, "link")));
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
      decision = el("div", { class: "decision", role: "group", "aria-label": `Décision pour ${slotLabel(match.slot)}` },
        el("small", {}, diffs.join(" ; ")),
        ["UPDATE", "KEEP"].map((choice) => el("button", {
          type: "button",
          class: state.decisions[index] === choice ? "primary small" : "small",
          "aria-pressed": state.decisions[index] === choice ? "true" : "false",
          "data-key": `decision-${index}-${choice}`,
          disabled: state.busy,
          onclick: () => { state.decisions[index] = choice; renderReview(); },
        }, choice === "UPDATE" ? "Mettre à jour" : "Garder l'existante")));
    }
    return el("tr", {},
      el("th", { scope: "row" }, slotLabel(match.slot)),
      el("td", {}, fmtDate(match.visit_date)),
      el("td", { class: `outcome o-${match.outcome}` }, OUTCOME[match.outcome]),
      el("td", {}, fmtField("gestational_age_days", fields.gestational_age_days)),
      el("td", {}, fmtField("weight_kg", fields.weight_kg)),
      el("td", {}, fmtBp(fields)),
      el("td", {}, fmtField("fundal_height_cm", fields.fundal_height_cm)),
      el("td", {}, decision));
  });
  node.append(el("div", { class: "table-wrap" }, el("table", { class: "summary-table" },
    el("caption", {}, "Visites qui seront enregistrées"),
    el("thead", {}, el("tr", {}, ["Visite", "Date", "Résultat", "AG", "Poids", "TA", "HU", "Décision"]
      .map((h) => el("th", { scope: "col" }, h)))),
    el("tbody", {}, rows))));

  const undecided = review.encounter_matches.filter((m) => m.outcome === "EXISTS_DIFFERENT" && !state.decisions[m.encounter_index]);
  if (undecided.length) {
    node.append(el("p", { class: "warn" },
      "Choisissez « Mettre à jour » ou « Garder l'existante » pour chaque visite signalée avant de confirmer."));
  }
  node.append(el("p", { class: "muted" },
    `Les champs non modifiés seront marqués « confirmés » par ${reviewer || "l'agent"}.`));
  node.append(el("div", { class: "actions" },
    button("Confirmer et enregistrer", confirmDocument, "primary big", { disabled: undecided.length > 0 })));
  return node;
}

function registrationMessage(d) {
  const registration = d.registration;
  return el("div", { class: "action-card done" },
    el("p", {}, `Enregistré par ${registration.registered_by} à ${fmtTime(registration.registered_at)}, `
      + `patiente ${registration.patient_id}${registration.patient_created ? " (créée)" : ""}.`),
    el("ul", {}, registration.visits.map((visit) => el("li", {},
      `${visit.visit_id} — ${fmtDate(visit.visit_date)} (${slotLabel(visit.slot)}) : ${OUTCOME[visit.outcome]}`))),
    el("div", { class: "actions" }, button("Voir le suivi de la patiente", async () => {
      state.patientId = registration.patient_id;
      await refreshTimeline(true);
      $(".timeline-panel").scrollIntoView({ block: "start" });
      $("#patient-select").focus();
    }, "primary")));
}

function renderPages(d) {
  const locked = d.document.status === "REGISTERED";
  return el("section", { class: "block", "aria-labelledby": "pages-title" },
    el("h3", { id: "pages-title" }, "Pages reçues"),
    el("div", { class: "pages" }, d.pages.map((page) => pageCard(d, page, locked))));
}

function pageCard(d, page, locked) {
  const pending = page.pending_retake_id;
  const isReplacement = d.retakes.some((r) => r.replacement_page_id === page.page_id);
  const name = basename(page.media_ref);
  return el("figure", { class: `page ${pending ? "retake-pending" : ""}` },
    el("a", { href: mediaUrl(page.media_ref), target: "_blank", rel: "noopener",
      "aria-label": `Ouvrir la page ${page.position} en grand (${name})` },
    el("img", { src: mediaUrl(page.media_ref), alt: `Page ${page.position} : ${name}` })),
    el("figcaption", {}, el("strong", {}, `Page ${page.position}`), ` · ${name}`),
    pending ? el("p", { class: "tag tag-warn" }, "Reprise demandée : en attente")
      : isReplacement ? el("p", { class: "tag tag-info" }, "Nouvelle photo (reprise)") : null,
    locked ? null : el("div", { class: "page-actions" },
      pending
        ? button("Annuler la reprise", () => cancelRetake(pending), "small", { key: `cancel-${pending}` })
        : button("Demander une reprise", () => requestRetake(page), "small",
          { key: `retake-${page.page_id}`, ariaLabel: `Demander une reprise de la page ${page.position}` }),
      pending ? null : moveSelect(d, page)));
}

function moveSelect(d, page) {
  const id = `move-${page.page_id}`;
  return el("div", { class: "move" },
    el("label", { for: id, class: "sr-only" }, `Déplacer la page ${page.position}`),
    el("select", {
      id,
      "data-key": id,
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
    d.move_targets.map((target) => el("option", { value: target }, `→ ${target}`))));
}

function renderRetakes(d) {
  if (!d.retakes.length) return null;
  return el("section", { class: "block", "aria-labelledby": "retakes-title" },
    el("h3", { id: "retakes-title" }, "Demandes de reprise"),
    el("ul", { class: "retakes" }, d.retakes.map((r) => el("li", { class: `retake rs-${r.status}` },
      el("div", { class: "retake-head" },
        el("strong", {}, `Page ${r.position}`),
        el("span", { class: `chip rs-${r.status}` }, RETAKE_STATUS[r.status]),
        el("span", { class: "muted" }, `${r.request_id} · demandée par ${r.requested_by} à ${fmtTime(r.requested_at)}`
          + (r.status === "CANCELLED" ? ` · annulée par ${r.closed_by}` : "")
          + (r.status === "FULFILLED" ? ` · reçue à ${fmtTime(r.closed_at)}` : ""))),
      el("div", { class: "retake-photos" },
        retakeThumb(r.original_media_ref, "Photo d'origine (conservée)"),
        r.replacement_media_ref ? el("span", { "aria-hidden": "true" }, "→") : null,
        r.replacement_media_ref ? retakeThumb(r.replacement_media_ref, "Nouvelle photo") : null)))));
}

function retakeThumb(ref, label) {
  return el("a", { class: "retake-thumb", href: mediaUrl(ref), target: "_blank", rel: "noopener" },
    el("img", { src: mediaUrl(ref), alt: "" }), el("span", {}, `${label} : ${basename(ref)}`));
}

function cellMeta(fv) {
  const verification = VERIFICATION[fv.verification.state];
  return [verification || FIELD_STATUS[fv.field_status], confidenceText(fv.confidence)];
}

function renderFieldGrid(d) {
  const draft = d.draft;
  const catalog = state.system.catalog;
  const editable = REVIEWABLE.has(d.document.status);
  const registered = d.document.status === "REGISTERED";

  const cell = (target, fv, label) => {
    const [status, confidence] = cellMeta(fv);
    const content = [el("span", { class: "val" }, fmtField(target.field, fv)),
      el("span", { class: "meta" }, status), el("span", { class: "meta" }, confidence)];
    const cls = `cell st-${fv.field_status} v-${fv.verification.state} ${sameTarget(state.edit, target) ? "editing" : ""}`;
    return el("td", { class: cls, title: fv.raw_text ? `lu « ${fv.raw_text} »` : null },
      editable ? el("button", {
        type: "button", class: "cell-btn", "data-key": targetKey(target),
        "aria-label": `${label} : ${fmtField(target.field, fv)}, ${status}, ${confidence}. Modifier`,
        onclick: () => { state.edit = target; state.error = null; renderReview(); },
      }, content) : content);
  };

  const header = el("table", { class: "grid" },
    el("caption", {}, "Champs du dossier"),
    el("tbody", {}, catalog.document_fields.map((name) =>
      el("tr", {}, el("th", { scope: "row" }, spec(name).label),
        cell({ scope: "document", encounter_index: null, field: name }, draft.document_fields[name], spec(name).label)))));

  const grid = el("table", { class: "grid" },
    el("caption", {}, "Champs par visite"),
    el("thead", {}, el("tr", {}, el("th", { scope: "col" }, "Champ"),
      draft.encounters.map((e) => el("th", { scope: "col" }, slotLabel(e.slot))))),
    el("tbody", {}, catalog.encounter_fields.map((name) => el("tr", {},
      el("th", { scope: "row" }, spec(name).label),
      draft.encounters.map((encounter, index) =>
        cell({ scope: "encounter", encounter_index: index, field: name }, encounter.fields[name],
          `${spec(name).label} — ${slotLabel(encounter.slot)}`))))));

  const pii = draft.pii_detected.length;
  return el("section", { class: "block", "aria-labelledby": "fields-title" },
    el("h3", { id: "fields-title" }, registered ? "Valeurs enregistrées" : "Valeurs du brouillon ",
      registered ? null : el("span", { class: "draft-badge" }, "non enregistrées")),
    el("p", { class: "muted" },
      `Extraction ${draft.extraction.extractor} ${draft.extraction.extractor_version} : `
      + `${plural(draft.encounters.length, "visite")} (${draft.encounters.map((e) => slotLabel(e.slot)).join(", ")}).`
      + (pii ? ` ${pii} donnée(s) personnelle(s) vue(s) sur la fiche, non extraite(s).` : "")
      + (editable ? " Cliquez sur une case (ou Tab puis Entrée) pour la modifier." : "")),
    el("div", { class: "legend", "aria-label": "Légende" },
      ["KNOWN", "NEEDS_REVIEW", "ILLEGIBLE", "NOT_PROVIDED"].map((s) => el("span", { class: `cell st-${s}` }, FIELD_STATUS[s])),
      el("span", { class: "cell v-CORRECTED" }, "corrigé"), el("span", { class: "cell v-CONFIRMED" }, "confirmé"),
      el("span", { class: "muted" }, "Confiance : élevée ≥ 75 %, moyenne 50–74 %, faible < 50 %")),
    el("div", { class: "grids" }, header, el("div", { class: "table-wrap" }, grid)));
}

function historyItem(event) {
  const detail = event.detail;
  switch (event.type) {
    case "FIELD_REVIEWED": {
      const label = spec(detail.field).label + (detail.slot ? ` (${slotLabel(detail.slot)})` : "");
      const value = detail.new.value !== null ? fmtValue(detail.field, detail.new.value) : FIELD_STATUS[detail.new.field_status];
      const verb = { CONFIRM: "Confirmé", CORRECT: "Corrigé", SET_STATUS: "Statut modifié" }[detail.action];
      return `${verb} : ${label} → ${value}`;
    }
    case "PATIENT_SELECTED":
      return { EXISTING: `Patiente choisie : ${detail.patient_id}`, NEW: "Nouvelle patiente", UNSURE: "Patiente incertaine (mis de côté)" }[detail.choice];
    case "PAGES_REGROUPED":
      return `Pages regroupées (${detail.page_id} : ${detail.from} → ${detail.to}). `
        + `${plural(detail.discarded_reviews, "vérification")} ${agree(detail.discarded_reviews, "annulée")}, nouvelle extraction.`;
    case "MANUAL_ENTRY_STARTED": return "Saisie manuelle démarrée.";
    case "RETAKE_REQUESTED": return `Reprise demandée pour la page ${detail.position} (${detail.request_id}).`;
    case "RETAKE_CANCELLED": return `Demande de reprise annulée pour la page ${detail.position} (${detail.request_id}).`;
    case "PAGE_REPLACED":
      return `Nouvelle photo reçue pour la page ${detail.position} : photo d'origine conservée, `
        + `${plural(detail.discarded_reviews, "vérification")} ${agree(detail.discarded_reviews, "annulée")}`
        + (detail.selection_discarded ? ", choix de patiente annulé" : "") + ", nouvelle extraction.";
    case "STATUS_CHANGED":
      return detail.to === "REGISTERED" ? "Dossier enregistré." : null;
    default: return null;
  }
}

function renderHistory(d) {
  const items = d.events.map((event) => [event, historyItem(event)]).filter(([, text]) => text);
  if (!items.length) return null;
  return el("details", { class: "block history" },
    el("summary", {}, `Historique du dossier (${items.length})`),
    el("ol", {}, items.map(([event, text]) => el("li", {},
      el("time", { datetime: event.at }, fmtTime(event.at)), el("span", { class: "actor" }, event.actor), el("span", {}, text)))));
}

// ---------------------------------------------------------------- timeline

function renderTimeline(patients, timeline) {
  const select = $("#patient-select");
  select.disabled = patients.length === 0;
  select.replaceChildren(...patients.map((p) =>
    el("option", { value: p.patient_id, selected: p.patient_id === state.patientId },
      `${p.patient_id} · ${plural(p.visit_count, "visite")}`)));
  const box = $("#timeline");
  if (!timeline) {
    box.replaceChildren(el("p", { class: "empty" },
      "Aucune patiente enregistrée pour l'instant. Les visites apparaissent ici après confirmation."));
    return;
  }
  const p = timeline.patient;
  const keys = [`N° fiche ${p.registry_file_number ?? "—"}`, `code ${p.midwife_patient_code ?? "—"}`];
  box.replaceChildren(
    el("p", { class: "muted" }, `${p.patient_id} — ${p.facility_name} · ${keys.join(" · ")} · ${plural(timeline.visits.length, "visite")}`
      + (p.flags.length ? ` · ${p.flags.join(", ")}` : "")),
    el("div", { class: "table-wrap" }, el("table", { class: "timeline" },
      el("caption", { class: "sr-only" }, `Visites enregistrées de ${p.patient_id}`),
      el("thead", {}, el("tr", {}, ["Date", "Visite", "AG", "Poids", "TA", "HU", "Syphilis", "VIH", "Sources"]
        .map((h) => el("th", { scope: "col" }, h)))),
      el("tbody", {}, timeline.visits.map((visit) => {
        const f = visit.fields;
        const fresh = visit.source_documents.includes(state.documentId);
        return el("tr", { class: fresh ? "fresh" : "" },
          el("td", {}, fmtDate(visit.visit_date)),
          el("td", {}, slotLabel(visit.slot)),
          el("td", {}, fmtField("gestational_age_days", f.gestational_age_days)),
          el("td", {}, fmtField("weight_kg", f.weight_kg)),
          el("td", {}, fmtBp(f)),
          el("td", {}, fmtField("fundal_height_cm", f.fundal_height_cm)),
          el("td", {}, fmtField("syphilis_test", f.syphilis_test)),
          el("td", {}, fmtField("hiv_test", f.hiv_test)),
          el("td", { class: "muted" }, visit.source_documents.join(", ")
            + (visit.revisions ? ` · ${plural(visit.revisions, "mise")} à jour` : ""),
            fresh ? el("span", { class: "tag tag-info" }, "dossier ouvert") : null));
      })))));
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
  $("#replay-last").addEventListener("click", replayLastMessage);
  $("#patient-select").addEventListener("change", (event) => { state.patientId = event.target.value; refreshTimeline(true); });
  $("#reset-demo").addEventListener("click", async () => {
    if (!window.confirm("Effacer toutes les données et recharger la visite antérieure de démonstration ?")) return;
    await api("POST", "/api/demo/reset", {});
    Object.assign(state, {
      documentId: null, detail: null, edit: null, decisions: {}, patientId: null, lastMessage: null, error: null, reply: null,
    });
    $("#replay-last").disabled = true;
    renderComposer();
    notify("Démo réinitialisée : une patiente avec 3 visites antérieures.", "success");
    await refreshAll(true);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    if (state.edit) { state.edit = null; state.error = null; renderReview(); $("#next-action-title")?.focus(); }
    else if (state.reply) cancelReply();
  });

  renderComposer();
  await refreshAll(true);
  setInterval(() => refreshAll(false), 1500);
}

init().catch((error) => notify(`Impossible de démarrer : ${error.message}`, "error"));
