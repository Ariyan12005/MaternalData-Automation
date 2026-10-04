"use strict";

const REVIEWABLE = new Set(["AI_PROCESSED", "NEEDS_REVIEW", "VALIDATED", "PATIENT_MATCHED", "DUPLICATE_SUSPECTED"]);

const DOC_STATUS = {
  CAPTURED: "Réception des pages", PENDING_AI: "En file de lecture", AI_PROCESSED: "Lu automatiquement",
  NEEDS_REVIEW: "À vérifier", VALIDATED: "Champs vérifiés", PATIENT_MATCHED: "Patiente choisie",
  REGISTERED: "Enregistré", PROCESSING_FAILED: "Échec de lecture", DUPLICATE_SUSPECTED: "Doublon suspecté",
};
const FIELD_STATUS = {
  KNOWN: "lu", NEEDS_REVIEW: "à vérifier", ILLEGIBLE: "illisible", NOT_PROVIDED: "non renseigné",
  UNKNOWN: "inconnu", NOT_APPLICABLE: "sans objet",
};
const RETAKE_STATUS = { PENDING: "En attente de la photo", FULFILLED: "Nouvelle photo reçue", CANCELLED: "Annulée" };
const OUTCOME = {
  NEW: "nouvelle visite", EXISTS_SAME: "déjà enregistrée (identique)",
  EXISTS_DIFFERENT: "déjà enregistrée, valeurs différentes", MISSING_DATE: "date manquante",
  CREATED: "créée", UNCHANGED: "déjà enregistrée, inchangée", UPDATED: "mise à jour",
  KEPT_EXISTING: "existante conservée",
};
const REASON = {
  NEEDS_REVIEW: "La lecture automatique n'est pas sûre de cette valeur.",
  ILLEGIBLE: "La lecture automatique n'arrive pas à lire ce qui est écrit.",
  REQUIRED_MISSING: "Ce champ est obligatoire pour enregistrer la visite.",
  DUPLICATE_ENCOUNTER_DATE: "Deux visites de ce dossier ont la même date.",
};
const PAGE_REASON = {
  UNKNOWN_LAYOUT: "Mise en page non reconnue : la lecture automatique ne sait pas quelle page du carnet c'est.",
  SECTION_UNCERTAIN: "Page reconnue avec un doute : vérifiez de quelle page du carnet il s'agit.",
  GRID_NOT_FOUND: "Page « Grossesse actuelle » dont le tableau des visites n'a pas été trouvé : ses visites ne sont "
    + "pas lues. Demandez une reprise, ou confirmez la section et saisissez ces visites à la main.",
  GRID_COLUMN_UNREAD: "Une colonne du tableau semble écrite mais rien n'y a été lu : elle n'est pas comptée comme "
    + "visite. Regardez la photo : demandez une reprise si une visite y figure, sinon confirmez la section.",
  PHOTO_UNUSABLE: "Photo inutilisable : rien n'a été lu sur cette page.",
  OCR_TIMEOUT: "La lecture de cette page a dépassé le temps maximal : rien n'a été lu.",
  OCR_FAILED: "La lecture automatique a échoué sur cette page : rien n'a été lu.",
};
const PAGE_ALERT = {
  UNKNOWN_LAYOUT: "mise en page non reconnue",
  SECTION_UNCERTAIN: "section reconnue avec un doute",
  GRID_NOT_FOUND: "tableau des visites introuvable",
  GRID_COLUMN_UNREAD: "une colonne écrite n'a pas été lue",
  PHOTO_UNUSABLE: "photo inutilisable",
  OCR_TIMEOUT: "lecture trop longue, rien n'a été lu",
  OCR_FAILED: "lecture impossible",
};
const PAGE_WARNING = { ORIENTATION_UNCERTAIN: "orientation de la photo incertaine" };
const RETRY_REASON = {
  OCR_DEPENDENCY_MISSING: "le moteur de lecture est incomplet sur ce poste",
  OCR_STOPPED: "le moteur de lecture s'est arrêté",
  EXTRACTOR_BACKING_OFF: "le service d'extraction est momentanément indisponible",
  EXTRACTION_ERROR: "erreur inattendue pendant la lecture",
  default: "lecture momentanément impossible",
};
const FLAG = {
  DIGIT_UNCLEAR: "chiffre peu lisible", MARKED_NOT_DONE: "noté « NF »", CROSSED_OUT: "case barrée",
  SECTION_NOT_CAPTURED: "page non photographiée", MANUAL_ENTRY: "saisie manuelle",
  KEY_MISMATCH: "une clé diffère", MULTIPLE_CANDIDATES: "plusieurs correspondances",
  NO_KEY_MATCH: "aucune clé de la fiche ne correspond", SLOT_DIFFERS: "colonne différente",
  OCR_NO_TEXT: "rien de lu (case non vérifiée)", MARKED_DASH: "tiret (non fait)",
  CELL_BLANK: "case vide (vérifiée sur la photo)", CELL_INK_UNREAD: "écriture présente mais non lue",
  CELL_OUT_OF_FRAME: "case hors de la photo", PAGE_NOT_READ: "une page n'a pas pu être lue",
  PHOTO_QUALITY_LOW: "photo de mauvaise qualité",
  OCR_CHARACTER_SUBSTITUTED: "lettre lue comme chiffre", OCR_CHARACTER_UNCLEAR: "trait ambigu (bordure ou 1)",
  OCR_EXTRA_TEXT: "texte en plus", OCR_UNPARSED: "lecture non reconnue", OCR_LOW_CONFIDENCE: "lecture incertaine",
  OCR_CONFIRM_REQUIRED: "à comparer avec la fiche", BP_UNITS_MIXED: "TA : unités mélangées (chiffre perdu ?)",
  OCR_LABEL_NOT_FOUND: "libellé introuvable", OCR_ROW_NOT_FOUND: "ligne introuvable",
  OCR_GRID_NOT_FOUND: "tableau des visites introuvable", OCR_NO_VISIT_FOUND: "aucune visite lue",
  DATE_ORDER: "dates des visites dans le désordre", DATE_INCONSISTENT_WITH_DDR: "date incohérente avec la DDR",
  GA_INCONSISTENT_WITH_DDR: "âge gestationnel incohérent avec la DDR",
  GA_INCONSISTENT_WITH_VISITS: "âge gestationnel incohérent avec les autres visites",
  CONFLICT_ACROSS_PAGES: "valeurs différentes selon les pages", OCR_TEXT_SPANS_COLUMNS: "texte à cheval sur deux colonnes",
  BP_LOOKS_LIKE_DATE: "TA : ressemble à une date", BP_ORDER_IMPLAUSIBLE: "TA : diastolique supérieure ou égale à la systolique",
  GA_FORMAT_AMBIGUOUS: "âge gestationnel décimal : semaines + jours ?",
  CHECKBOX_UNCLEAR: "case peu nette (cochée ?)", CHECKBOX_NOT_FOUND: "case introuvable sur la photo",
  CHECKBOX_MULTIPLE_MARKED: "plusieurs cases cochées", CHECKBOX_NONE_MARKED: "aucune case cochée",
  LAB_WITHOUT_VISIT: "examen dans une colonne sans visite",
};
const CONFLICT_FLAGS = new Set(["CONFLICT_ACROSS_PAGES", "CHECKBOX_MULTIPLE_MARKED"]);
const CONSISTENCY_FLAGS = new Set(["DATE_ORDER", "DATE_INCONSISTENT_WITH_DDR", "GA_INCONSISTENT_WITH_DDR",
  "GA_INCONSISTENT_WITH_VISITS", "BP_UNITS_MIXED", "BP_LOOKS_LIKE_DATE", "BP_ORDER_IMPLAUSIBLE", "LAB_WITHOUT_VISIT"]);
const HINT = {
  date: "jj/mm/aaaa", gestational_age: "ex. 28SA+1j ou 28 SA 1 j", decimal: "ex. 63", int: "ex. 27",
  bp: "ex. 12 ou 120", enum: "négatif ou positif", digits: "chiffres", code: "ex. 164125", text: "",
};
const CHOICE_LABEL = {
  MARKED: "case cochée", UNMARKED: "case vide",
  VAGINAL_NON_INSTRUMENTAL: "voie basse non instrumentale", VAGINAL_INSTRUMENTAL: "voie basse instrumentale",
  CESAREAN_PLANNED: "césarienne programmée", CESAREAN_EMERGENCY: "césarienne en urgence",
  FEMALE: "féminin", MALE: "masculin",
  EXCLUSIVE_BREASTFEEDING: "exclusivement au sein", ARTIFICIAL: "artificiel", MIXED: "mixte",
};
const UNIT_LABEL = { years: "ans", days: "jours" };
const MARK_STATE = { MARKED: "cochée", UNMARKED: "vide", UNCLEAR: "peu nette", BOX_NOT_FOUND: "introuvable",
  LABEL_NOT_FOUND: "libellé introuvable" };

/** Review groups, in reading order. Extended sections are listed with the group that shows them. */
const GROUPS = [
  { id: "identity", title: "Clés d'identification" },
  { id: "history", title: "Identification et antécédents", sections: ["patient", "pregnancy", "previous_deliveries"] },
  { id: "visits", title: "Grossesse actuelle et visites prénatales", sections: ["visit_labs"] },
  { id: "delivery", title: "Accouchement", sections: ["delivery"] },
  { id: "newborn", title: "Nouveau-né", sections: ["newborns", "newborn_consultations"] },
];
/** Read on the pregnancy grid page, so shown with the visits rather than with the history. */
const WITH_VISITS = new Set(["height_cm"]);
const PAGE_GROUP = { cover: "identity", identification: "history", delivery: "delivery", newborn: "newborn" };
const STEPS = [["read", "Lecture des pages"], ["fields", "Vérification"], ["patient", "Patiente"],
  ["confirm", "Résumé"], ["done", "Enregistré"]];
const STEP_OF = { COLLECTING: "read", WAITING_AI: "read", WAITING_RETAKE: "read", FAILED: "read", FIELD: "fields",
  PATIENT: "patient", EXISTING_VISITS: "confirm", CONFIRM: "confirm", DONE: "done" };
const STALE_MESSAGE = "Le dossier a changé pendant votre action (nouvelle lecture, reprise de photo ou autre agent). "
  + "La dernière version est affichée : vérifiez-la, puis recommencez.";

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
  open: null,
  edit: null,
  returnFocus: null,
  visitIndex: null,
  openDetails: new Set(),
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

/** A <details> block that stays open or closed across re-renders. */
function keptDetails(key, cls, summary, ...children) {
  return el("details", {
    class: cls, open: state.openDetails.has(key),
    ontoggle: (event) => { if (event.target.open) state.openDetails.add(key); else state.openDetails.delete(key); },
  }, el("summary", { "data-key": `summary-${key}` }, summary), ...children);
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

const spec = (name) => state.system.catalog.fields[name] || state.system.catalog.extended.fields[name];
const slotLabel = (slot) => state.system.catalog.slots[slot] || slot;
const basename = (ref) => ref.split("/").pop();
const mediaUrl = (ref) => "/media/" + encodeURIComponent(ref);
const plural = (n, word) => `${n} ${word}${n > 1 ? "s" : ""}`;
const agree = (n, word) => `${word}${n > 1 ? "s" : ""}`;
const unitText = (unit) => (unit ? UNIT_LABEL[unit] || unit : null);

/** The extractor's own score, shown as a number only: it is not calibrated and is not a probability of being right. */
function scoreText(fv) {
  if (fv.verification.state === "CORRECTED") return "saisi par l'agent";
  if (fv.validation_flags.includes("MANUAL_ENTRY")) return "saisie manuelle";
  if (fv.source?.checkboxes) return "pas de score (case à cocher)";
  if (fv.confidence === null || fv.confidence === undefined) return "pas de score";
  return `${Math.round(fv.confidence * 100)} % (indicatif)`;
}

/** Status in words: what the reviewer must know about this value, never by colour alone. */
function statusInfo(fv, extended = false) {
  const v = fv.verification;
  const who = v.by ? ` par ${v.by}` : "";
  const optional = extended ? " (facultatif)" : "";
  if (v.state === "CORRECTED") {
    return { text: (fv.validation_flags.includes("MANUAL_ENTRY") ? "Saisi" : "Corrigé") + who, tone: "ok", todo: false };
  }
  if (v.state === "CONFIRMED") {
    const base = fv.field_status === "KNOWN" ? "Vérifié" : `${capital(FIELD_STATUS[fv.field_status])}, confirmé`;
    return { text: base + who, tone: "ok", todo: false };
  }
  const flags = fv.validation_flags;
  if (flags.includes("SECTION_NOT_CAPTURED")) {
    return { text: "Page non photographiée" + (fv.field_status === "NEEDS_REVIEW" ? ", à saisir" : ""), tone: "todo",
      todo: fv.field_status === "NEEDS_REVIEW" };
  }
  switch (fv.field_status) {
    case "KNOWN": return { text: "Lu, non vérifié", tone: "unverified", todo: false };
    case "NEEDS_REVIEW":
      return { text: (fv.value === null && flags.includes("MANUAL_ENTRY") ? "À saisir" : "À vérifier") + optional,
        tone: "todo", todo: true };
    case "ILLEGIBLE": return { text: "Illisible, à vérifier" + optional, tone: "todo", todo: true };
    case "NOT_PROVIDED": return { text: "Non renseigné sur la fiche, non vérifié", tone: "absent", todo: false };
    case "NOT_APPLICABLE": return { text: "Sans objet, non vérifié", tone: "absent", todo: false };
    default: return { text: "Inconnu, non vérifié", tone: "absent", todo: false };
  }
}

const capital = (text) => text.charAt(0).toUpperCase() + text.slice(1);

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
  if (s.kind === "choice") return CHOICE_LABEL[value] || value;
  return s.unit ? `${value} ${unitText(s.unit)}` : String(value);
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
  if (target.scope === "extended") {
    const data = draft.extended[target.section];
    return (target.item_index === null || target.item_index === undefined ? data : data[target.item_index].fields)[target.field];
  }
  return target.scope === "document"
    ? draft.document_fields[target.field]
    : draft.encounters[target.encounter_index].fields[target.field];
}

/** The field a target points to, or null when the draft changed and it no longer exists. */
function fieldAt(draft, target) {
  try {
    return getField(draft, target) ?? null;
  } catch {
    return null;
  }
}

const sameTarget = (a, b) =>
  a && b && a.scope === b.scope && a.field === b.field && (a.encounter_index ?? null) === (b.encounter_index ?? null)
  && (a.section ?? null) === (b.section ?? null) && (a.item_index ?? null) === (b.item_index ?? null);
const targetKey = (t) => `cell-${t.scope}-${t.section ?? ""}-${t.item_index ?? t.encounter_index ?? "doc"}-${t.field}`;

function targetOf(item) {
  const target = { scope: item.scope, encounter_index: item.encounter_index ?? null, field: item.field };
  if (item.scope === "extended") Object.assign(target, { section: item.section, item_index: item.item_index ?? null });
  return target;
}

/** Which item of a list section (previous delivery, visit, newborn, consultation) a field belongs to. */
function itemLabel(section, item) {
  const key = item?.[state.system.catalog.extended.sections[section].item_key];
  if (key === undefined) return "";
  if (section === "previous_deliveries") return `accouchement antérieur ${key}`;
  if (section === "visit_labs") return slotLabel(key);
  if (section === "newborns") return `nouveau-né ${key}`;
  return state.system.catalog.extended.periods[key] || String(key);
}

function extendedItem(draft, target) {
  const data = draft.extended?.[target.section];
  return Array.isArray(data) ? data[target.item_index] : null;
}

/** A manual visit opened for an unreadable grid page names that page: several such pages each open one. */
function visitLabel(draft, encounter) {
  if (encounter.slot !== "MANUAL") return slotLabel(encounter.slot);
  const ref = encounter.fields.visit_date.source?.page_ref;
  const index = ref ? draft.pages.findIndex((page) => page.page_ref === ref) : -1;
  return index >= 0 ? `${slotLabel("MANUAL")} — page ${index + 1}` : slotLabel("MANUAL");
}

function fieldTitle(draft, target) {
  const label = spec(target.field).label;
  if (target.scope === "encounter") return `${label} — ${visitLabel(draft, draft.encounters[target.encounter_index])}`;
  if (target.scope === "extended") {
    const where = itemLabel(target.section, extendedItem(draft, target));
    return label + (where ? ` — ${where}` : "");
  }
  return label;
}

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

/** Page geometry: which group a booklet section feeds, and whether it is read at all. */
function pageGroup(section) {
  if (!section) return null;
  if (section.startsWith("current_pregnancy")) return "visits";
  return PAGE_GROUP[section] ?? null;
}

function isUnsupportedPage(page) {
  return page.read !== false && !page.review_reason && Boolean(page.section) && page.section !== "unknown"
    && pageGroup(page.section) === null;
}

function sectionLabel(section) {
  return state.system.catalog.page_sections[section] || section;
}

/** Bbox of the image that was read, mapped onto the photo as shown (the reader may have turned the page). */
function displayBox(bbox, drafted) {
  if (!bbox || !drafted) return null;
  const [x0, y0, x1, y1] = bbox;
  switch (drafted.image?.rotated_ccw || 0) {
    case 0: return bbox;
    case 90: return [1 - y1, x0, 1 - y0, x1];
    case 180: return [1 - x1, 1 - y1, 1 - x0, 1 - y0];
    case 270: return [y0, 1 - x1, y1, 1 - x0];
    default: return null;
  }
}

function boxStyle([x0, y0, x1, y1]) {
  return `left:${x0 * 100}%;top:${y0 * 100}%;width:${(x1 - x0) * 100}%;height:${(y1 - y0) * 100}%`;
}

// ---------------------------------------------------------------- actions

async function act(fn, { focus = "#next-action-title" } = {}) {
  if (state.busy) return;
  state.busy = true;
  state.error = null;
  $("#review").setAttribute("aria-busy", "true");
  let ok = false;
  try {
    await fn();
    ok = true;
    Object.assign(state, { edit: null, open: null, visitIndex: null });
  } catch (error) {
    if (error.code === "STALE_REVISION") {
      Object.assign(state, { edit: null, open: null, error: STALE_MESSAGE });
    } else {
      state.error = error.message;
    }
  } finally {
    state.busy = false;
    $("#review").removeAttribute("aria-busy");
    await refreshAll(true);
  }
  if (!ok) $("#action-error")?.focus();
  else if (focus) document.querySelector(focus)?.focus();
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

function setPageSection(page, section) {
  if (!window.confirm("Choisir la section relance la lecture du dossier et annule ses vérifications. Continuer ?")) {
    return renderReview();
  }
  return act(() => api("POST", `/api/pages/${page.page_id}/section`, {
    section: section || null, expected_revision: revision(),
  }));
}

/** Why the extractor could not use this page's photo (a retake reason code), if it said so. */
function retakeReason(d, page) {
  const drafted = d.draft?.pages[page.position - 1];
  if (drafted && drafted.page_ref === page.media_ref) return drafted.retake_reason || null;
  return d.document.failure_pages.find((p) => p.page_id === page.page_id)?.reason || null;
}

function requestRetake(page) {
  const why = state.system.retake_reasons[retakeReason(state.detail, page)];
  const text = `Merci de reprendre la photo de la page ${page.position}` + (why ? ` : ${why}` : ".");
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
  Object.assign(state, {
    documentId, detail: null, open: null, edit: null, visitIndex: null, decisions: {}, changingPatient: false, error: null,
  });
  state.signatures.detail = null;
  renderReview();
  refreshDetail(true);
  renderQueue(state.queue || []);
}

/** Shows one field's question at the top of the review, from a list row or an alert. */
function openField(target, returnKey) {
  Object.assign(state, { open: target, edit: null, error: null, returnFocus: returnKey });
  if (target.scope === "encounter") state.visitIndex = target.encounter_index;
  if (target.section === "visit_labs") {
    const slot = extendedItem(state.detail.draft, target)?.slot;
    const index = state.detail.draft.encounters.findIndex((e) => e.slot === slot);
    if (index >= 0) state.visitIndex = index;
  }
  renderReview();
  $("#field-question-title")?.focus();
}

function closeField() {
  const key = state.returnFocus;
  Object.assign(state, { open: null, edit: null, error: null, returnFocus: null });
  renderReview();
  const back = key && $("#review").querySelector(`[data-key="${CSS.escape(key)}"]`);
  (back || $("#next-action-title"))?.focus();
}

function startEdit(target) {
  state.edit = target;
  state.error = null;
  renderReview();
}

function cancelEdit() {
  state.edit = null;
  state.error = null;
  renderReview();
  $("#field-question-title")?.focus();
}

function selectVisit(index, focusTab = false) {
  state.visitIndex = index;
  renderReview();
  if (focusTab) $(`#visit-tab-${index}`)?.focus();
}

function openPreview(ref, box, caption) {
  const dialog = $("#page-dialog");
  state.previewReturn = document.activeElement;
  $("#page-dialog-title").textContent = caption;
  $("#page-dialog-body").replaceChildren(
    el("div", { class: "preview-image full" },
      el("img", { src: mediaUrl(ref), alt: caption }),
      box ? el("span", { class: "source-box", "aria-hidden": "true", style: boxStyle(box) }) : null),
    el("p", { class: "muted" }, box ? "L'encadré orange montre où la valeur a été lue."
      : "Aucun emplacement précis n'est disponible pour cette page."),
    el("a", { href: mediaUrl(ref), target: "_blank", rel: "noopener" }, "Ouvrir l'image d'origine dans un nouvel onglet"));
  dialog.showModal();
  $("#page-dialog-close").focus();
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
    [d.document_id, d.status, d.blocking_count, d.page_count, d.pending_retakes, d.extracting, d.retrying])
    .concat(state.system?.ai_available));
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
    JSON.stringify(detail.document.extraction_progress), JSON.stringify(detail.document.extraction_retry),
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
    case "PENDING_AI":
      if (doc.extracting) return { text: "Lecture en cours", action: false };
      if (doc.retrying) return { text: "Échec momentané : nouvel essai automatique", action: false };
      return { text: state.system?.ai_available === false ? "En pause : extraction désactivée" : "En attente de lecture",
        action: false };
    case "PROCESSING_FAILED": return { text: "À faire : reprise ou saisie manuelle", action: true };
    case "AI_PROCESSED":
    case "NEEDS_REVIEW": return { text: `À faire : ${plural(doc.blocking_count, "point")} à vérifier`, action: true };
    case "VALIDATED": return { text: "À faire : choisir la patiente", action: true };
    case "DUPLICATE_SUSPECTED": return { text: "À faire : décider (doublon suspecté)", action: true };
    case "PATIENT_MATCHED": return { text: "À faire : relire et enregistrer", action: true };
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
    pruneTargets(d);
    nodes = [renderDocumentHeader(d), renderSteps(d), renderNextAction(d), renderAlerts(d), renderContext(d),
      d.draft ? renderGroups(d) : null, renderPages(d), renderRetakes(d), renderHistory(d)];
  }
  replaceKeepingFocus(root, nodes, "#next-action-title");
}

/** Drops an open question or correction whose field no longer exists (new extraction, registration). */
function pruneTargets(d) {
  const reviewable = d.draft && REVIEWABLE.has(d.document.status);
  if (state.open && !(reviewable && fieldAt(d.draft, state.open))) state.open = null;
  if (state.edit && !(reviewable && fieldAt(d.draft, state.edit))) state.edit = null;
}

/** The field the review question is about: one opened by the reviewer, else the first blocking field. */
function currentTarget(d) {
  if (state.open) return state.open;
  const first = d.review?.blocking[0];
  return d.next_step === "FIELD" && first && first.scope !== "page" ? targetOf(first) : null;
}

function renderDocumentHeader(d) {
  const doc = d.document;
  let record = el("span", { class: "draft-badge" }, "Rien d'enregistré pour l'instant");
  if (doc.status === "REGISTERED") record = el("span", { class: "saved-badge" }, "Visites enregistrées");
  else if (d.draft) record = el("span", { class: "draft-badge" }, "Brouillon, non enregistré");
  return el("div", { class: "doc-header" },
    el("h2", { id: "review-title" }, `Dossier ${doc.document_id}`),
    chip(doc.status),
    record,
    doc.grouping_status === "PROVISIONAL" && doc.status !== "REGISTERED"
      ? el("span", { class: "chip grouping-PROVISIONAL" }, "Regroupement des pages provisoire") : null,
    el("span", { class: "muted" }, plural(d.pages.length, "page")));
}

function renderSteps(d) {
  const at = STEPS.findIndex(([id]) => id === (STEP_OF[d.next_step] || "read"));
  return el("ol", { class: "steps", "aria-label": "Étapes du dossier" }, STEPS.map(([, label], index) => {
    const step = d.next_step === "DONE" || index < at ? "done" : index === at ? "current" : "todo";
    return el("li", { class: `step ${step}`, "aria-current": step === "current" ? "step" : null },
      el("span", { class: "step-n", "aria-hidden": "true" }, step === "done" ? "✓" : index + 1),
      el("span", {}, label),
      el("span", { class: "sr-only" }, { done: " (terminée)", current: " (en cours)", todo: " (à venir)" }[step]));
  }));
}

function queuePosition(documentId) {
  const waiting = (state.queue || []).filter((doc) => doc.status === "PENDING_AI")
    .map((doc) => doc.document_id).sort();
  return waiting.indexOf(documentId) + 1;
}

function nextActionCopy(d) {
  const blocking = d.review?.blocking.length ?? 0;
  const pendingPages = d.retakes.filter((r) => r.status === "PENDING").map((r) => r.position).join(", ");
  if (state.open && REVIEWABLE.has(d.document.status)) {
    return [statusInfo(getField(d.draft, state.open)).todo ? "Vérifier ce champ" : "Revoir ce champ", null];
  }
  switch (d.next_step) {
    case "COLLECTING":
      return ["Attendre les autres pages",
        `Les photos arrivent encore. La lecture démarre ${state.system.grouping_window_seconds} s après la dernière photo.`];
    case "WAITING_AI": {
      const progress = d.document.extraction_progress;
      const retry = d.document.extraction_retry;
      if (progress) {
        return ["Lecture des pages en cours",
          `Page ${Math.min(progress.pages_done + 1, progress.pages)} sur ${progress.pages}. Aucune action requise : `
          + "les valeurs s'afficheront ici à la fin de la lecture."];
      }
      if (!state.system.ai_available) {
        return ["En pause : extraction désactivée",
          "Rien n'est perdu : le dossier sera lu dès que l'extraction sera réactivée (contrôle de démonstration en haut)."];
      }
      if (retry) {
        return ["Nouvel essai automatique prévu",
          `La dernière tentative a échoué à ${fmtTime(retry.failed_at)} : ${RETRY_REASON[retry.reason] || RETRY_REASON.default} `
          + `(${plural(retry.attempts, "tentative")}). Le dossier reste en file et sera relu automatiquement ; rien `
          + "n'est perdu. Si l'échec se répète, prévenez la personne responsable du poste de lecture."];
      }
      const ahead = queuePosition(d.document.document_id) - 1;
      return ["En file de lecture",
        (ahead > 0 ? `${plural(ahead, "dossier")} avant celui-ci. ` : "") + "Aucune action requise."];
    }
    case "WAITING_RETAKE":
      return [`Attendre la nouvelle photo (page ${pendingPages})`,
        "La sage-femme a reçu la demande. L'enregistrement est bloqué jusqu'à la réception ; "
        + "la nouvelle photo relancera la lecture et les vérifications."];
    case "FAILED":
      return ["Lecture impossible : choisir une solution",
        `Raison : ${(d.document.failure_message || d.document.failure_reason || "inconnue").replace(/\.+$/, "")}. `
        + "Demandez une reprise si une photo est "
        + "illisible, saisissez les données à la main, ou déplacez les pages (« Pages reçues ») si elles concernent "
        + "plusieurs femmes."];
    case "FIELD": return [`Vérifier les points signalés (${blocking} restant${blocking > 1 ? "s" : ""})`, null];
    case "PATIENT": return ["Choisir la patiente", null];
    case "EXISTING_VISITS":
      return ["Décider pour les visites déjà enregistrées",
        "Certaines visites existent déjà avec d'autres valeurs : rien ne sera modifié sans votre choix."];
    case "CONFIRM": return ["Relire le résumé puis enregistrer", null];
    case "DONE": return ["Terminé : visites enregistrées", null];
    default: return [d.next_step, null];
  }
}

function renderNextAction(d) {
  const [title, text] = nextActionCopy(d);
  const waiting = ["COLLECTING", "WAITING_AI", "WAITING_RETAKE"].includes(d.next_step);
  return el("section", { id: "next-action", class: `next-action step-${d.next_step}`, "aria-labelledby": "next-action-title" },
    el("p", { class: "eyebrow" }, "Prochaine action"),
    el("h3", { id: "next-action-title", tabindex: "-1" }, title),
    text ? el("p", { class: "next-text", "aria-live": waiting ? "polite" : null }, text) : null,
    state.error ? el("div", { id: "action-error", class: "alert", role: "alert", tabindex: "-1" },
      el("strong", {}, "Action impossible : "), state.error) : null,
    currentStep(d));
}

function currentStep(d) {
  const step = d.next_step;
  if (state.open && REVIEWABLE.has(d.document.status)) {
    const pending = d.review.blocking.find((item) => sameTarget(state.open, item));
    return fieldQuestion(d, pending || { ...state.open, reason: null }, d.review.blocking.length);
  }
  if (step === "WAITING_AI" && d.document.extraction_progress) {
    const p = d.document.extraction_progress;
    return el("div", { class: "progress-row" },
      el("label", { for: "extract-progress" }, "Progression"),
      el("progress", { id: "extract-progress", max: p.pages, value: p.pages_done }, `${p.pages_done}/${p.pages}`),
      el("span", { class: "muted" }, `${p.pages_done} / ${plural(p.pages, "page")} ${agree(p.pages_done, "lue")}`));
  }
  if (step === "WAITING_RETAKE") {
    return el("div", { class: "actions" }, d.retakes.filter((r) => r.status === "PENDING").map((r) =>
      button(`Annuler la demande (page ${r.position})`, () => cancelRetake(r.request_id), "", { key: `cancel-${r.request_id}` })));
  }
  if (step === "FAILED") {
    const retakes = d.document.failure_pages.filter((p) => state.system.retake_reasons[p.reason])
      .map((p) => d.pages.find((page) => page.page_id === p.page_id)).filter(Boolean);
    return el("div", { class: "actions" },
      retakes.map((page) => button(`Demander une reprise (page ${page.position})`, () => requestRetake(page), "primary",
        { key: `failed-retake-${page.page_id}` })),
      button("Saisie manuelle", startManualEntry, retakes.length ? "" : "primary"));
  }
  if (step === "FIELD" && d.review.blocking[0].scope === "page") {
    return pageQuestion(d, d.review.blocking[0], d.review.blocking.length);
  }
  if (step === "FIELD") return fieldQuestion(d, d.review.blocking[0], d.review.blocking.length);
  if (step === "PATIENT" || (state.changingPatient && (step === "CONFIRM" || step === "EXISTING_VISITS"))) {
    return patientQuestion(d);
  }
  if (step === "CONFIRM" || step === "EXISTING_VISITS") return summary(d);
  if (step === "DONE") return registrationMessage(d);
  return null;
}

function docPageFor(d, pageRef) {
  return pageRef ? d.pages.find((page) => page.media_ref === pageRef) || null : null;
}

function pageNumber(d, pageRef) {
  return d.draft.pages.findIndex((p) => p.page_ref === pageRef) + 1;
}

function fieldQuestion(d, item, remaining = 0) {
  const target = targetOf(item);
  const fv = getField(d.draft, target);
  const s = spec(item.field);
  const extendedField = target.scope === "extended";
  const info = statusInfo(fv, extendedField);
  const page = docPageFor(d, fv.source?.page_ref);
  const unverified = fv.verification.state === "UNVERIFIED";

  let read = "rien de lu";
  if (fv.raw_text) {
    const ambiguous = fv.value === null && fv.validation_flags.includes("OCR_CHARACTER_UNCLEAR");
    read = `« ${fv.raw_text} »` + (fv.value === null ? (ambiguous ? " (deux lectures possibles)" : " (non reconnu)") : "");
  }
  let reason = item.reason ? REASON[item.reason] : null;
  if (fv.validation_flags.includes("MANUAL_ENTRY") && fv.value === null && item.reason === "NEEDS_REVIEW") {
    reason = "Saisie manuelle : recopiez la valeur depuis la fiche papier, ou indiquez qu'elle est illisible ou non "
      + "renseignée.";
  }
  if (!reason && extendedField) {
    reason = "Donnée complémentaire : elle ne bloque pas l'enregistrement et reste « non vérifiée » tant que personne "
      + "ne la confirme ou ne la corrige.";
  }
  if (!reason) reason = "Comparez avec la photo, puis confirmez ou corrigez.";

  const where = s.source ? s.source.label + (s.source.note ? `. ${s.source.note}` : "")
    : [fv.source?.row && `ligne « ${fv.source.row} »`, fv.source?.column && `colonne « ${fv.source.column} »`]
      .filter(Boolean).join(", ");
  const editing = sameTarget(state.edit, target);
  const locked = d.document.status === "REGISTERED";

  const actions = el("div", { class: "actions" },
    fv.value !== null ? button("Confirmer la valeur", () => reviewField(target, "CONFIRM"), "primary") : null,
    button(fv.value !== null ? "Corriger" : "Saisir la valeur", () => startEdit(target), fv.value === null ? "primary" : ""),
    s.required ? null : fv.field_status === "ILLEGIBLE"
      ? (unverified ? button("Confirmer « illisible »", () => reviewField(target, "CONFIRM")) : null)
      : button("Illisible", () => reviewField(target, "SET_STATUS", { field_status: "ILLEGIBLE" })),
    s.required ? null : fv.field_status === "NOT_PROVIDED"
      ? (unverified ? button("Confirmer « non renseigné »", () => reviewField(target, "CONFIRM")) : null)
      : button("Non renseigné sur la fiche", () => reviewField(target, "SET_STATUS", { field_status: "NOT_PROVIDED" })),
    item.reason ? null : button("Fermer", closeField, "ghost", { key: "close-field" }));

  const retake = page && !locked
    ? page.pending_retake_id
      ? el("p", { class: "tag tag-warn" }, `Reprise de la page ${page.position} en attente`)
      : el("p", { class: "retake-line" }, "La photo ne permet pas de décider ? ",
        button(`Demander une reprise de la page ${page.position}`, () => requestRetake(page), "link",
          { key: `q-retake-${page.page_id}` }))
    : null;

  return el("div", { class: "action-card question" },
    el("h4", { id: "field-question-title", class: "q-title", tabindex: "-1" }, fieldTitle(d.draft, target)),
    remaining ? el("p", { class: "muted" }, `${plural(remaining, "point")} à vérifier avant l'enregistrement.`) : null,
    el("p", { class: "reason" }, reason),
    el("div", { class: "question-grid" },
      el("div", { class: "question-main" },
        el("dl", { class: "facts" },
          el("dt", {}, "Lu sur la photo"), el("dd", {}, read),
          el("dt", {}, "Valeur"), el("dd", {}, fv.value !== null ? fmtValue(item.field, fv.value) : "aucune"),
          s.unit ? [el("dt", {}, "Unité"), el("dd", {}, unitText(s.unit))] : null,
          el("dt", {}, "Statut"), el("dd", {}, info.text),
          el("dt", {}, "Score de lecture"), el("dd", {}, scoreText(fv)),
          fv.validation_flags.length ? [el("dt", {}, "Signalements"),
            el("dd", {}, fv.validation_flags.map((f) => FLAG[f] || f).join(", "))] : null,
          fv.source?.readings ? [el("dt", {}, "Lu sur chaque page"), el("dd", {}, el("ul", { class: "readings" },
            fv.source.readings.map((r) => el("li", {}, `Page ${pageNumber(d, r.page_ref)} : ${readingText(item.field, r)}`))))]
            : null,
          fv.source?.normalization && fv.verification.state !== "CORRECTED"
            ? [el("dt", {}, "Conversion"), el("dd", {}, normalizationText(fv))] : null,
          fv.source?.checkboxes ? [el("dt", {}, "Cases lues"), el("dd", {}, fv.source.checkboxes
            .map((b) => `${b.label} : ${MARK_STATE[b.state] || b.state}`).join(" ; "))] : null,
          where ? [el("dt", {}, "Sur la fiche"), el("dd", {}, where)] : null),
        editing ? correctionForm(target) : actions,
        retake),
      fv.source ? pagePreview(d, fv.source) : el("p", { class: "muted preview-none" },
        "Aucune photo associée à ce champ (page non photographiée ou saisie manuelle).")));
}

function readingText(name, r) {
  if (r.value !== null) return `« ${r.raw_text ?? "—"} », soit ${fmtValue(name, r.value)}`;
  const flags = r.validation_flags.map((f) => FLAG[f] || f).join(", ");
  return (r.raw_text ? `« ${r.raw_text} » non lu` : FIELD_STATUS[r.field_status]) + (flags ? ` (${flags})` : "");
}

function normalizationText(fv) {
  const n = fv.source.normalization;
  if (n.from === "cmHg") return `« ${fv.raw_text} » noté en cmHg, converti en mmHg (×${n.factor})`;
  if (n.from === "weeks+days") return `${n.weeks} SA + ${n.days} j, soit ${fv.value} jours`;
  return `${n.from} → ${n.to}`;
}

/** The page a value was read on, with the reading's place outlined when the extractor gave coordinates. */
function pagePreview(d, source) {
  const ref = source.page_ref;
  const number = pageNumber(d, ref) || docPageFor(d, ref)?.position || "?";
  const drafted = d.draft.pages.find((p) => p.page_ref === ref);
  const box = displayBox(source.bbox, drafted);
  const caption = `Page ${number}`;
  const where = [source.row && `ligne « ${source.row} »`, source.column && `colonne « ${source.column} »`]
    .filter(Boolean).join(", ");
  return el("figure", { class: "preview" },
    box && drafted.image ? cropView(ref, box, drafted.image) : null,
    el("div", { class: "preview-image" },
      el("img", { src: mediaUrl(ref), alt: `${caption} (${basename(ref)})` }),
      box ? el("span", { class: "source-box", "aria-hidden": "true", style: boxStyle(box) }) : null),
    el("figcaption", {}, box
      ? `${caption} : l'encadré orange montre où la valeur a été lue.`
      : `${caption} : emplacement précis non disponible${where ? ` (${where})` : ""}.`),
    button("Agrandir la page", () => openPreview(ref, box, caption), "small", { key: `zoom-${ref}` }));
}

/** A close-up of the outlined place. The image is scaled with percentages, so it needs the page's proportions. */
function cropView(ref, box, image) {
  const turned = (image.rotated_ccw || 0) % 180 !== 0;
  const aspect = turned ? image.width / image.height : image.height / image.width;
  let [x0, y0, x1, y1] = [box[0] - 0.08, box[1] - 0.04, box[2] + 0.08, box[3] + 0.04];
  if (x1 - x0 < 0.3) [x0, x1] = [(x0 + x1) / 2 - 0.15, (x0 + x1) / 2 + 0.15];
  const shift = (a, b) => (a < 0 ? [0, b - a] : b > 1 ? [a - (b - 1), 1] : [a, b]);
  [x0, x1] = shift(x0, x1);
  [y0, y1] = shift(y0, y1);
  [x0, y0, x1, y1] = [Math.max(0, x0), Math.max(0, y0), Math.min(1, x1), Math.min(1, y1)];
  const w = x1 - x0;
  const h = y1 - y0;
  const inner = [(box[0] - x0) / w, (box[1] - y0) / h, (box[2] - x0) / w, (box[3] - y0) / h];
  return el("div", { class: "crop", style: `aspect-ratio:${(w / (h * aspect)).toFixed(4)}`, "aria-hidden": "true" },
    el("img", { src: mediaUrl(ref), alt: "",
      style: `width:${(100 / w).toFixed(3)}%;transform:translate(${(-x0 * 100).toFixed(3)}%,${(-y0 * 100).toFixed(3)}%)` }),
    el("span", { class: "source-box", style: boxStyle(inner) }));
}

function sectionSelect(d, page, id) {
  const sections = state.system.catalog.page_sections;
  return el("select", {
    id, "data-key": id, disabled: state.busy,
    onchange: (event) => setPageSection(page, event.target.value),
  },
  el("option", { value: "", selected: !page.section_hint }, "— détection automatique —"),
  Object.entries(sections).map(([value, label]) =>
    el("option", { value, selected: page.section_hint === value }, label)));
}

function pageQuestion(d, item, remaining = 0) {
  const page = d.pages[item.page_index];
  const drafted = d.draft.pages[item.page_index];
  const why = drafted.retake_reason ? state.system.retake_reasons[drafted.retake_reason] : null;
  const detected = drafted.section !== "unknown" ? sectionLabel(drafted.section) : null;
  const id = `section-${page.page_id}`;
  return el("div", { class: "action-card question" },
    el("h4", { id: "field-question-title", class: "q-title", tabindex: "-1" }, `Page ${page.position}`),
    remaining ? el("p", { class: "muted" }, `${plural(remaining, "point")} à vérifier avant l'enregistrement.`) : null,
    el("p", { class: "reason" }, PAGE_REASON[item.reason] || item.reason),
    el("div", { class: "question-grid" },
      el("div", { class: "question-main" },
        el("dl", { class: "facts" },
          detected ? [el("dt", {}, "Section proposée"), el("dd", {}, detected)] : null,
          why ? [el("dt", {}, "Message de reprise"), el("dd", {}, why)] : null),
        el("div", { class: "actions" },
          el("label", { for: id }, "Cette page est : "), sectionSelect(d, page, id),
          page.pending_retake_id ? null : button("Demander une reprise", () => requestRetake(page),
            drafted.read ? "" : "primary", { key: `page-retake-${page.page_id}` })),
        el("p", { class: "hint" }, "Choisir la section relance la lecture avec ce choix ; les champs non lus "
          + "resteront à vérifier un par un.")),
      pagePreview(d, { page_ref: page.media_ref })));
}

function correctionForm(target) {
  const s = spec(target.field);
  const id = `correction-${target.field}`;
  const hintId = `${id}-hint`;
  const input = s.kind === "choice"
    ? el("select", { id, required: true },
      el("option", { value: "" }, "— choisir —"),
      s.choices.map((value) => el("option", { value }, CHOICE_LABEL[value] || value)))
    : el("input", { id, type: "text", placeholder: HINT[s.kind] || "", autocomplete: "off", required: true,
      "aria-describedby": hintId });
  setTimeout(() => input.focus(), 0);
  return el("form", {
    class: "correction",
    onsubmit: (event) => {
      event.preventDefault();
      reviewField(target, "CORRECT", { value: input.value });
    },
  },
  el("label", { for: id }, `Nouvelle valeur pour « ${s.label} »`),
  el("p", { id: hintId, class: "muted" }, [HINT[s.kind] ? `Format : ${HINT[s.kind]}.` : null,
    s.unit ? `Unité : ${unitText(s.unit)}.` : null, "Échap pour annuler."].filter(Boolean).join(" ")),
  el("div", { class: "correction-row" },
    input,
    button("Valider la correction", null, "primary", { type: "submit" }),
    button("Annuler", cancelEdit, "ghost")));
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
    button("Nouvelle patiente", () => selectPatient("NEW"), candidates.length ? "" : "primary"),
    button("Je ne sais pas", () => selectPatient("UNSURE"), "ghost"),
    state.changingPatient ? button("Annuler", () => { state.changingPatient = false; renderReview(); }, "ghost") : null));
  node.append(el("p", { class: "hint" }, (suggested ? "Proposition automatique à confirmer. " : "")
    + "Ce choix n'enregistre rien : un résumé suivra avant l'enregistrement."));
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

/** v1.0 fields of the document and its visits, with their targets, in review order. */
function v1Entries(draft) {
  const entries = state.system.catalog.document_fields.map((field) => ({
    target: { scope: "document", encounter_index: null, field }, fv: draft.document_fields[field] }));
  draft.encounters.forEach((encounter, index) => state.system.catalog.encounter_fields.forEach((field) =>
    entries.push({ target: { scope: "encounter", encounter_index: index, field }, fv: encounter.fields[field] })));
  return entries;
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

  const decides = review.encounter_matches.some((match) => match.outcome === "EXISTS_DIFFERENT");
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
      decides ? el("td", {}, decision) : null);
  });
  const headers = ["Visite", "Date", "Résultat", "AG", "Poids", "TA", "HU", ...(decides ? ["Décision"] : [])];
  node.append(el("div", { class: "table-wrap" }, el("table", { class: "summary-table" },
    el("caption", {}, "Visites qui seront enregistrées"),
    el("thead", {}, el("tr", {}, headers.map((h) => el("th", { scope: "col" }, h)))),
    el("tbody", {}, rows))));

  const empty = v1Entries(d.draft).filter(({ fv }) => fv.value === null);
  if (empty.length) {
    node.append(keptDetails("summary-empty", "summary-list",
      `${plural(empty.length, "champ")} sans valeur ${empty.length > 1 ? "seront enregistrés" : "sera enregistré"} tel${empty.length > 1 ? "s" : ""} quel${empty.length > 1 ? "s" : ""}`,
      el("ul", {}, empty.map(({ target, fv }) => el("li", {}, `${fieldTitle(d.draft, target)} : ${statusInfo(fv).text}`)))));
  }

  const undecided = review.encounter_matches.filter((m) => m.outcome === "EXISTS_DIFFERENT" && !state.decisions[m.encounter_index]);
  if (undecided.length) {
    node.append(el("p", { class: "warn" },
      "Choisissez « Mettre à jour » ou « Garder l'existante » pour chaque visite signalée avant de confirmer."));
  }
  node.append(el("p", { class: "muted" },
    `Les valeurs lues et non modifiées des visites seront marquées « vérifiées » par ${reviewer || "l'agent"}.`));
  const extendedUnverified = extendedTargets(d.draft).filter(({ fv }) => fv.verification.state === "UNVERIFIED").length;
  if (extendedUnverified) {
    node.append(el("p", { class: "muted" },
      `${plural(extendedUnverified, "donnée complémentaire")} non ${agree(extendedUnverified, "vérifiée")} `
      + `${extendedUnverified > 1 ? "restent" : "reste"} avec le dossier, sans être confirmée${extendedUnverified > 1 ? "s" : ""} `
      + "automatiquement ni enregistrée comme visite."));
  }
  node.append(el("div", { class: "actions" },
    button("Confirmer et enregistrer", confirmDocument, "primary big", { disabled: undecided.length > 0 })));
  return node;
}

function registrationMessage(d) {
  const registration = d.registration;
  const extendedUnverified = d.draft
    ? extendedTargets(d.draft).filter(({ fv }) => fv.verification.state === "UNVERIFIED").length : 0;
  return el("div", { class: "action-card done" },
    el("p", {}, `Enregistré par ${registration.registered_by} à ${fmtTime(registration.registered_at)}, `
      + `patiente ${registration.patient_id}${registration.patient_created ? " (créée)" : ""}.`),
    registration.visits.length ? el("ul", {}, registration.visits.map((visit) => el("li", {},
      `${visit.visit_id} — ${fmtDate(visit.visit_date)} (${slotLabel(visit.slot)}) : ${OUTCOME[visit.outcome]}`)))
      : el("p", { class: "muted" }, "Aucune visite enregistrée : ce dossier ne contenait pas de visite prénatale."),
    extendedUnverified ? el("p", { class: "muted" }, `${plural(extendedUnverified, "donnée complémentaire")} `
      + `${extendedUnverified > 1 ? "restent" : "reste"} non ${agree(extendedUnverified, "vérifiée")} dans le dossier.`) : null,
    el("div", { class: "actions" }, button("Voir le suivi de la patiente", async () => {
      state.patientId = registration.patient_id;
      await refreshTimeline(true);
      $(".timeline-panel").scrollIntoView({ block: "start" });
      $("#patient-select").focus();
    }, "primary")));
}

// ---------------------------------------------------------------- alerts and context

function allTargets(draft) {
  return [...v1Entries(draft), ...extendedTargets(draft)];
}

function renderAlerts(d) {
  if (!d.draft) return null;
  const editable = REVIEWABLE.has(d.document.status);
  const items = [];
  if (d.draft.extraction.extractor === "manual") {
    items.push({ tone: "info", text: "Saisie manuelle : rien n'a été lu automatiquement ; chaque valeur est à saisir." });
  }
  d.draft.pages.forEach((page, index) => {
    const n = index + 1;
    if (page.review_reason) {
      items.push({ tone: "warn", text: `Page ${n} : ${PAGE_ALERT[page.review_reason] || page.review_reason}.` });
    } else if (page.grid?.found === false) {
      items.push({ tone: "warn", text: `Page ${n} : tableau des visites introuvable ; saisissez sa visite dans `
        + `« ${slotLabel("MANUAL")} — page ${n} », ou demandez une reprise.` });
    } else if (isUnsupportedPage(page)) {
      items.push({ tone: "info", text: `Page ${n} (${sectionLabel(page.section)}) : section non prise en charge `
        + "par la lecture automatique ; rien n'en est extrait." });
    }
    for (const warning of page.warnings || []) {
      const text = state.system.retake_reasons[warning] || PAGE_WARNING[warning] || warning;
      items.push({ tone: "info", text: `Page ${n} : ${text}` });
    }
  });
  for (const { target, fv } of allTargets(d.draft)) {
    if (fv.verification.state !== "UNVERIFIED") continue;
    const conflict = fv.validation_flags.find((f) => CONFLICT_FLAGS.has(f));
    const odd = fv.validation_flags.find((f) => CONSISTENCY_FLAGS.has(f));
    if (conflict || odd) {
      items.push({ tone: conflict ? "warn" : "info", target,
        text: `${conflict ? "Conflit" : "Incohérence"} — ${fieldTitle(d.draft, target)} : ${FLAG[conflict || odd]}.` });
    }
  }
  if (!items.length) return null;
  const shown = items.slice(0, 8);
  return el("section", { class: "block alerts", "aria-labelledby": "alerts-title" },
    el("h3", { id: "alerts-title" }, `À signaler (${items.length})`),
    el("ul", {}, shown.map((item, index) => el("li", { class: `alert-item a-${item.tone}` },
      el("span", { class: "alert-kind" }, item.tone === "warn" ? "Attention" : "Info"),
      el("span", {}, item.text),
      item.target && editable ? button("Ouvrir", () => openField(item.target, `alert-${index}`), "small",
        { key: `alert-${index}`, ariaLabel: `Ouvrir : ${fieldTitle(d.draft, item.target)}` }) : null))),
    items.length > shown.length
      ? el("p", { class: "muted" }, `et ${items.length - shown.length} autre(s), visibles dans les sections ci-dessous.`) : null);
}

function shortValue(name, fv) {
  if (!fv) return "non lu";
  return fv.value !== null ? fmtValue(name, fv.value) : FIELD_STATUS[fv.field_status];
}

function renderContext(d) {
  if (!d.draft) return null;
  const draft = d.draft;
  const review = d.review;
  const selection = review?.selection;
  let patient = "à choisir";
  if (d.registration) patient = `${d.registration.patient_id} (enregistrée)`;
  else if (selection?.choice === "EXISTING") patient = `${selection.patient_id} (choisie)`;
  else if (selection?.choice === "NEW") patient = "nouvelle patiente (choisie)";
  else if (review?.suggested_patient_id) patient = `${review.suggested_patient_id} proposée, à confirmer`;
  const linked = selection?.patient_id ?? (selection ? null : review?.suggested_patient_id);
  const candidate = linked ? review?.candidates.find((c) => c.patient_id === linked) : null;
  const doc = draft.document_fields;
  const pregnancy = draft.extended?.pregnancy;
  const facts = [
    ["Patiente", patient],
    ["N° de la fiche · code", `${shortValue("registry_file_number", doc.registry_file_number)} · `
      + shortValue("midwife_patient_code", doc.midwife_patient_code)],
    ["DDR", shortValue("last_menstrual_period", doc.last_menstrual_period)],
    ["Visites lues dans ce dossier", draft.encounters.length
      ? `${draft.encounters.length} (${draft.encounters.map((e) => visitLabel(draft, e)).join(", ")})` : "aucune"],
  ];
  if (candidate && !d.registration) {
    facts.push(["Déjà enregistré pour cette patiente", `${plural(candidate.visit_count, "visite")}`
      + (candidate.last_visit_date ? `, dernière le ${fmtDate(candidate.last_visit_date)}` : "")]);
  }
  if (pregnancy && (pregnancy.gravidity || pregnancy.parity)) {
    facts.push(["Gestation · parité", `${shortValue("gravidity", pregnancy.gravidity)} · `
      + shortValue("parity", pregnancy.parity)]);
  }
  return el("section", { class: "block context", "aria-labelledby": "context-title" },
    el("h3", { id: "context-title" }, "Contexte de la grossesse ",
      el("span", { class: "muted" }, d.registration ? "(valeurs du dossier)" : "(valeurs du brouillon, à vérifier)")),
    el("dl", { class: "context-facts" }, facts.map(([term, value]) => el("div", {}, el("dt", {}, term), el("dd", {}, value)))));
}

// ---------------------------------------------------------------- field groups

function extendedTargets(draft) {
  const ext = draft.extended;
  if (!ext) return [];
  const targets = [];
  for (const [section, info] of Object.entries(state.system.catalog.extended.sections)) {
    if (!(section in ext)) continue;
    const items = info.item_key ? ext[section].map((item, index) => [index, item, item.fields]) : [[null, null, ext[section]]];
    for (const [index, item, fields] of items) {
      for (const field of info.fields) {
        if (field in fields) targets.push({ target: { scope: "extended", section, item_index: index, field }, item, fv: fields[field] });
      }
    }
  }
  return targets;
}

function fieldRow(d, entry, ctx, label = fieldTitle(d.draft, entry.target)) {
  const { target, fv } = entry;
  const info = statusInfo(fv, target.scope === "extended");
  const current = sameTarget(target, ctx.current);
  const key = targetKey(target);
  const verb = info.todo ? "Vérifier" : "Revoir";
  let value = fv.value !== null ? fmtValue(target.field, fv.value) : "—";
  if (fv.value === null && fv.raw_text) value = `— (lu « ${fv.raw_text} »)`;
  return el("tr", { class: `field-row t-${info.tone} ${current ? "current" : ""}`, "aria-current": current ? "true" : null },
    el("th", { scope: "row" }, label, current ? el("span", { class: "sr-only" }, " (question en cours)") : null),
    el("td", { "data-label": "Valeur" }, value),
    el("td", { "data-label": "Statut" }, el("span", { class: `status t-${info.tone}` }, info.text)),
    el("td", { "data-label": "Score de lecture" }, scoreText(fv)),
    el("td", { class: "row-action" }, ctx.editable
      ? button(verb, () => openField(target, key), info.todo ? "small todo-btn" : "small",
        { key, ariaLabel: `${verb} : ${label}` }) : null));
}

function fieldTable(d, caption, entries, ctx, labelOf = undefined) {
  return el("div", { class: "table-wrap" }, el("table", { class: "fields" },
    el("caption", {}, caption),
    el("thead", {}, el("tr", {},
      el("th", { scope: "col" }, "Champ"), el("th", { scope: "col" }, "Valeur"), el("th", { scope: "col" }, "Statut"),
      el("th", { scope: "col" }, "Score de lecture"), el("th", { scope: "col" }, el("span", { class: "sr-only" }, "Action")))),
    el("tbody", {}, entries.map((entry) => fieldRow(d, entry, ctx, labelOf ? labelOf(entry) : undefined)))));
}

function countsText(entries) {
  const todo = entries.filter(({ target, fv }) => statusInfo(fv, target.scope === "extended").todo).length;
  const unverified = entries.filter(({ fv }) => fv.verification.state === "UNVERIFIED" && fv.field_status === "KNOWN").length;
  if (todo) return { text: `${todo} à vérifier`, tone: "todo" };
  if (unverified) return { text: `${unverified} ${agree(unverified, "lue")}, non ${agree(unverified, "vérifiée")}`, tone: "unverified" };
  return { text: "rien en attente", tone: "ok" };
}

function group(id, title, entries, ...body) {
  const counts = countsText(entries);
  return el("section", { class: "group", "aria-labelledby": `group-${id}` },
    el("div", { class: "group-head" },
      el("h4", { id: `group-${id}` }, title),
      entries.length ? el("span", { class: `count t-${counts.tone}` }, counts.text) : null),
    ...body);
}

function renderGroups(d) {
  const draft = d.draft;
  const registered = d.document.status === "REGISTERED";
  const ctx = { editable: REVIEWABLE.has(d.document.status), current: currentTarget(d) };
  const captured = new Set(draft.pages.map((p) => pageGroup(p.section)).filter(Boolean));
  const ext = extendedTargets(draft);
  const inGroup = (id) => ext.filter(({ target }) => GROUPS.find((g) => g.id === id).sections?.includes(target.section)
    && (id === "visits" || !WITH_VISITS.has(target.field)));

  const built = [
    ["identity", identityGroup(d, ctx)],
    ["history", extendedGroup(d, ctx, "history", inGroup("history"), captured,
      "La page d'identification a été reçue, mais ses antécédents ne sont pas lus automatiquement pour cette mise "
      + "en page : rien n'en est extrait.")],
    ["visits", visitsGroup(d, ctx, ext, captured)],
    ["delivery", extendedGroup(d, ctx, "delivery", inGroup("delivery"), captured,
      "La page d'accouchement a été reçue, mais elle n'est pas lue automatiquement pour cette mise en page.")],
    ["newborn", extendedGroup(d, ctx, "newborn", inGroup("newborn"), captured,
      "La page du nouveau-né a été reçue, mais elle n'est pas lue automatiquement pour cette mise en page.",
      "Nouveau-né 1 : le seul bloc nouveau-né de la page d'accouchement (les naissances multiples ne sont pas prises "
      + "en charge). L'allaitement est celui du jour de la consultation.")],
  ];
  const shown = built.filter(([, node]) => node);
  const absent = built.filter(([, node]) => !node).map(([id]) => GROUPS.find((g) => g.id === id).title);
  const pii = draft.pii_detected.length;

  return el("section", { class: `block draft-area ${registered ? "saved" : ""}`, "aria-labelledby": "fields-title" },
    el("h3", { id: "fields-title" }, registered ? "Données du dossier " : "Valeurs du brouillon ",
      registered ? el("span", { class: "saved-badge" }, "visites enregistrées")
        : el("span", { class: "draft-badge" }, "non enregistrées")),
    el("p", { class: "muted" }, registered
      ? "Les visites sont enregistrées (voir « Suivi de la patiente »). Les données complémentaires restent avec le "
        + "dossier ; celles qui n'ont pas été vérifiées le restent."
      : "Rien n'est enregistré avant « Confirmer et enregistrer ». Les points « À vérifier » des visites et des clés "
        + "bloquent l'enregistrement ; les données complémentaires ne le bloquent pas."
        + (pii ? ` ${plural(pii, "donnée personnelle")} vue${pii > 1 ? "s" : ""} sur la fiche, non extraite${pii > 1 ? "s" : ""}.` : "")),
    keptDetails("legend", "legend", "Comment lire les statuts",
      el("ul", {},
        el("li", {}, el("strong", {}, "À vérifier"), " : la lecture automatique hésite ; votre décision est nécessaire."),
        el("li", {}, el("strong", {}, "Lu, non vérifié"), " : lu automatiquement ; personne ne l'a encore confirmé."),
        el("li", {}, el("strong", {}, "Non renseigné sur la fiche"), " : la case est vide sur le papier."),
        el("li", {}, el("strong", {}, "Illisible"), " : quelque chose est écrit mais ne peut pas être lu."),
        el("li", {}, el("strong", {}, "Page non photographiée"), " : la page qui porte ce champ manque au dossier."),
        el("li", {}, el("strong", {}, "Section non prise en charge"), " : la page est reçue mais pas lue automatiquement ; "
          + "rien n'en est extrait."),
        el("li", {}, el("strong", {}, "Score de lecture"), " : donné par le moteur de lecture ; il est indicatif, n'est "
          + "pas calibré et ne dit pas si la valeur est juste."))),
    shown.map(([, node]) => node),
    absent.length ? el("p", { class: "muted" }, `Sans page dans ce dossier (non affiché) : ${absent.join(", ")}.`) : null);
}

function identityGroup(d, ctx) {
  const entries = ["registry_file_number", "midwife_patient_code", "facility_name"].map((field) => ({
    target: { scope: "document", encounter_index: null, field }, fv: d.draft.document_fields[field] }));
  return group("identity", "Clés d'identification", entries,
    el("p", { class: "muted" }, "Elles servent à retrouver la patiente dans l'établissement. Le CIN n'est jamais utilisé."),
    fieldTable(d, "Clés lues sur la fiche", entries, ctx));
}

function extendedGroup(d, ctx, id, entries, captured, notice, intro = null) {
  const title = GROUPS.find((g) => g.id === id).title;
  if (!entries.length) {
    if (!captured.has(id)) return null;
    return group(id, title, [], el("p", { class: "notice" }, el("strong", {}, "Section non prise en charge : "), notice));
  }
  const sections = [...new Set(entries.map(({ target }) => target.section))];
  const catalog = state.system.catalog.extended.sections;
  return group(id, title, entries,
    intro ? el("p", { class: "muted" }, intro) : null,
    sections.map((section) => fieldTable(d, catalog[section].label,
      entries.filter(({ target }) => target.section === section), ctx)));
}

function visitsGroup(d, ctx, ext, captured) {
  const draft = d.draft;
  const encounters = draft.encounters;
  const labs = ext.filter(({ target }) => target.section === "visit_labs");
  const height = ext.filter(({ target }) => WITH_VISITS.has(target.field));
  if (!encounters.length && !labs.length && !captured.has("visits")) return null;
  const ddr = { target: { scope: "document", encounter_index: null, field: "last_menstrual_period" },
    fv: draft.document_fields.last_menstrual_period };
  const context = [ddr, ...height];
  const slotOf = (entry) => entry.item.slot;
  const visitEntries = (index) => [
    ...state.system.catalog.encounter_fields.map((field) => ({
      target: { scope: "encounter", encounter_index: index, field }, fv: encounters[index].fields[field] })),
    ...labs.filter((entry) => slotOf(entry) === encounters[index].slot),
  ];
  const orphanLabs = labs.filter((entry) => !encounters.some((e) => e.slot === slotOf(entry)));
  const all = [...context, ...encounters.flatMap((_e, index) => visitEntries(index)), ...orphanLabs];

  const body = [fieldTable(d, "Grossesse en cours", context, ctx)];
  if (!encounters.length) {
    body.push(el("p", { class: "notice" }, "Aucune visite prénatale lue sur ces pages : rien ne sera enregistré comme visite."));
  } else {
    const current = ctx.current;
    let preferred = state.visitIndex;
    if (preferred === null && current?.scope === "encounter") preferred = current.encounter_index;
    if (preferred === null && current?.section === "visit_labs") {
      preferred = encounters.findIndex((e) => e.slot === extendedItem(draft, current)?.slot);
    }
    if (preferred === null || preferred < 0) {
      preferred = encounters.findIndex((_e, i) => visitEntries(i).some(({ target, fv }) =>
        statusInfo(fv, target.scope === "extended").todo));
    }
    const index = Math.min(Math.max(preferred ?? 0, 0), encounters.length - 1);
    const todoAt = (i) => visitEntries(i).filter(({ target, fv }) => statusInfo(fv, target.scope === "extended").todo).length;
    const last = encounters.length - 1;
    const onKey = (event, i) => {
      const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: last }[event.key];
      if (next === undefined) return;
      event.preventDefault();
      selectVisit(Math.min(Math.max(next, 0), last), true);
    };
    body.push(
      el("div", { class: "visit-nav" },
        el("div", { class: "tabs", role: "tablist", "aria-label": "Visites de ce dossier" }, encounters.map((encounter, i) => {
          const todo = todoAt(i);
          const date = encounter.fields.visit_date.value;
          return el("button", {
            type: "button", role: "tab", id: `visit-tab-${i}`, class: `tab ${todo ? "has-todo" : ""}`,
            "aria-selected": i === index ? "true" : "false", "aria-controls": "visit-panel",
            tabindex: i === index ? "0" : "-1", "data-key": `visit-tab-${i}`,
            onclick: () => selectVisit(i), onkeydown: (event) => onKey(event, i),
          },
          el("span", { class: "tab-title" }, visitLabel(draft, encounter)),
          el("span", { class: "tab-meta" }, date ? fmtDate(date) : "date à vérifier"),
          el("span", { class: todo ? "tab-todo" : "tab-meta" }, todo ? `${todo} à vérifier` : "rien à vérifier"));
        })),
        el("div", { class: "visit-steps" },
          button("← Visite précédente", () => selectVisit(index - 1, false), "small",
            { disabled: index === 0, key: "visit-prev" }),
          el("span", { class: "muted" }, `Visite ${index + 1} sur ${encounters.length}`),
          button("Visite suivante →", () => selectVisit(index + 1, false), "small",
            { disabled: index === last, key: "visit-next" }))),
      el("div", { id: "visit-panel", role: "tabpanel", "aria-labelledby": `visit-tab-${index}`, class: "visit-panel" },
        fieldTable(d, `${visitLabel(draft, encounters[index])} : champs de la visite et examens`, visitEntries(index), ctx,
          ({ target }) => spec(target.field).label)),
      keptDetails("overview", "overview", "Comparer toutes les visites (tableau)", overviewGrid(d, ctx)));
  }
  if (orphanLabs.length) {
    body.push(fieldTable(d, "Examens écrits dans une colonne sans visite", orphanLabs, ctx));
  }
  return group("visits", GROUPS.find((g) => g.id === "visits").title, all, ...body);
}

function overviewGrid(d, ctx) {
  const draft = d.draft;
  const cell = (target, fv, label) => {
    const info = statusInfo(fv);
    const cls = `cell t-${info.tone} ${sameTarget(target, ctx.current) ? "current" : ""}`;
    const content = [el("span", { class: "val" }, fmtField(target.field, fv)), el("span", { class: "meta" }, info.text)];
    return el("td", { class: cls },
      ctx.editable ? el("button", {
        type: "button", class: "cell-btn", "data-key": `grid-${targetKey(target)}`,
        "aria-label": `${label} : ${fmtField(target.field, fv)}, ${info.text}. Ouvrir`,
        onclick: () => openField(target, `grid-${targetKey(target)}`),
      }, content) : content);
  };
  return el("div", { class: "table-wrap" }, el("table", { class: "grid" },
    el("caption", { class: "sr-only" }, "Toutes les visites du dossier"),
    el("thead", {}, el("tr", {}, el("th", { scope: "col" }, "Champ"),
      draft.encounters.map((e) => el("th", { scope: "col" }, visitLabel(draft, e))))),
    el("tbody", {}, state.system.catalog.encounter_fields.map((name) => el("tr", {},
      el("th", { scope: "row" }, spec(name).label),
      draft.encounters.map((encounter, index) =>
        cell({ scope: "encounter", encounter_index: index, field: name }, encounter.fields[name],
          `${spec(name).label} — ${visitLabel(draft, encounter)}`)))))));
}

// ---------------------------------------------------------------- pages, retakes, history

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
    el("button", { type: "button", class: "page-thumb", "data-key": `thumb-${page.page_id}`,
      "aria-label": `Agrandir la page ${page.position} (${name})`,
      onclick: () => openPreview(page.media_ref, null, `Page ${page.position}`) },
    el("img", { src: mediaUrl(page.media_ref), alt: "" })),
    el("figcaption", {}, el("strong", {}, `Page ${page.position}`), ` · ${name}`),
    pending ? el("p", { class: "tag tag-warn" }, "Reprise demandée : en attente")
      : isReplacement ? el("p", { class: "tag tag-info" }, "Nouvelle photo (reprise)") : null,
    pageSection(d, page, locked),
    locked ? null : el("div", { class: "page-actions" },
      pending
        ? button("Annuler la reprise", () => cancelRetake(pending), "small", { key: `cancel-${pending}` })
        : button("Demander une reprise", () => requestRetake(page), "small",
          { key: `retake-${page.page_id}`, ariaLabel: `Demander une reprise de la page ${page.position}` }),
      pending ? null : moveSelect(d, page)));
}

/** Section of a page read by local OCR: detected (with its confidence) or chosen by a reviewer. */
function pageSection(d, page, locked) {
  const drafted = d.draft?.pages[page.position - 1];
  if (!drafted || drafted.page_ref !== page.media_ref || !drafted.section_source) return null;
  const label = state.system.catalog.page_sections[drafted.section] || "section inconnue";
  let how = drafted.section_source === "reviewer" ? "choisie par l'agent"
    : !drafted.read ? "page non lue" : drafted.section_confidence === "high" ? "détectée" : "détectée avec un doute";
  if (isUnsupportedPage(drafted)) how += ", non lue automatiquement";
  const id = `page-section-${page.page_id}`;
  return el("div", { class: `page-section ${drafted.review_reason ? "tag-warn" : ""}` },
    el("label", { for: id }, `Section : ${label} (${how})`),
    locked ? null : sectionSelect(d, page, id));
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
        if (!window.confirm("Déplacer cette page relance la lecture des dossiers concernés et annule leurs vérifications. Continuer ?")) {
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
        el("span", { class: "muted" }, `demandée par ${r.requested_by} à ${fmtTime(r.requested_at)}`
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

function historyItem(event) {
  const detail = event.detail;
  switch (event.type) {
    case "FIELD_REVIEWED": {
      const label = spec(detail.field).label + (detail.slot ? ` (${slotLabel(detail.slot)})` : "")
        + (detail.section ? ` (${state.system.catalog.extended.sections[detail.section].label}`
          + `${detail.item_index !== null && detail.item_index !== undefined ? `, élément ${detail.item_index + 1}` : ""})` : "");
      const value = detail.new.value !== null ? fmtValue(detail.field, detail.new.value) : FIELD_STATUS[detail.new.field_status];
      const verb = { CONFIRM: "Confirmé", CORRECT: "Corrigé", SET_STATUS: "Statut modifié" }[detail.action];
      return `${verb} : ${label} → ${value}`;
    }
    case "PATIENT_SELECTED":
      return { EXISTING: `Patiente choisie : ${detail.patient_id}`, NEW: "Nouvelle patiente", UNSURE: "Patiente incertaine (mis de côté)" }[detail.choice];
    case "PAGES_REGROUPED":
      return `Pages regroupées (${detail.page_id} : ${detail.from} → ${detail.to}). `
        + `${plural(detail.discarded_reviews, "vérification")} ${agree(detail.discarded_reviews, "annulée")}, nouvelle lecture.`;
    case "MANUAL_ENTRY_STARTED": return "Saisie manuelle démarrée.";
    case "RETAKE_REQUESTED": return `Reprise demandée pour la page ${detail.position}.`;
    case "RETAKE_CANCELLED": return `Demande de reprise annulée pour la page ${detail.position}.`;
    case "PAGE_REPLACED":
      return `Nouvelle photo reçue pour la page ${detail.position} : photo d'origine conservée, `
        + `${plural(detail.discarded_reviews, "vérification")} ${agree(detail.discarded_reviews, "annulée")}`
        + (detail.selection_discarded ? ", choix de patiente annulé" : "") + ", nouvelle lecture.";
    case "STATUS_CHANGED":
      return detail.to === "REGISTERED" ? "Dossier enregistré." : null;
    default: return null;
  }
}

function renderHistory(d) {
  const items = d.events.map((event) => [event, historyItem(event)]).filter(([, text]) => text);
  if (!items.length) return null;
  return keptDetails("history", "block history", `Historique du dossier (${items.length})`,
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
    notify(event.target.checked ? "Extraction réactivée : la file d'attente est traitée."
      : "Extraction en pause : les dossiers restent en file d'attente.");
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
      documentId: null, detail: null, open: null, edit: null, visitIndex: null, decisions: {}, patientId: null,
      lastMessage: null, error: null, reply: null,
    });
    $("#replay-last").disabled = true;
    renderComposer();
    notify("Démo réinitialisée : une patiente avec 3 visites antérieures.", "success");
    await refreshAll(true);
  });
  const dialog = $("#page-dialog");
  $("#page-dialog-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener("close", () => {
    const back = state.previewReturn;
    state.previewReturn = null;
    (back?.isConnected ? back : $("#next-action-title"))?.focus();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || dialog.open) return;
    if (state.edit) cancelEdit();
    else if (state.open) closeField();
    else if (state.reply) cancelReply();
  });

  renderComposer();
  await refreshAll(true);
  setInterval(() => refreshAll(false), 1500);
}

init().catch((error) => notify(`Impossible de démarrer : ${error.message}`, "error"));
