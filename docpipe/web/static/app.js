"use strict";

const POLL_MS = 2000;
const STEPS = ["Extract", "Refine", "Save"];
// state -> number of completed steps (out of 3)
const STEP_PROGRESS = {
  QUEUED: 0,
  PENDING: 0,
  EXTRACTED: 1,
  RESTRUCTURED: 2,
  COMPLETED: 3,
};
const WORKING_STATES = new Set([
  "QUEUED",
  "PENDING",
  "EXTRACTED",
  "RESTRUCTURED",
]);

const els = {
  dropzone: document.getElementById("dropzone"),
  fileInput: document.getElementById("file-input"),
  rows: document.getElementById("doc-rows"),
  toasts: document.getElementById("toasts"),
  conn: document.getElementById("conn-status"),
  connLabel: document.getElementById("conn-label"),
  statTotal: document.getElementById("stat-total"),
  statWorking: document.getElementById("stat-working"),
  statCompleted: document.getElementById("stat-completed"),
  statFailed: document.getElementById("stat-failed"),
  preview: document.getElementById("preview"),
  previewTitle: document.getElementById("preview-title"),
  previewBody: document.getElementById("preview-body"),
  previewClose: document.getElementById("preview-close"),
  dlMarkdown: document.getElementById("dl-markdown"),
  dlMetadata: document.getElementById("dl-metadata"),
  tabs: document.querySelectorAll(".tab"),
  modal: document.getElementById("modal"),
  modalTitle: document.getElementById("modal-title"),
  modalText: document.getElementById("modal-text"),
  modalConfirm: document.getElementById("modal-confirm"),
  modalCancel: document.getElementById("modal-cancel"),
};

const state = {
  docs: new Map(), // id -> doc
  pending: new Set(), // ids with an action in flight (skip actions repaint)
  failCount: 0,
  activePreview: null, // { id, tab }
};

// --- Utilities ---------------------------------------------------------------

function escapeHtml(value) {
  return window.MarkdownRenderer.escapeHtml(value == null ? "" : value);
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return d.toLocaleDateString();
}

function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = message;
  els.toasts.appendChild(el);
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 250);
  }, 3500);
}

function setConnection(ok) {
  if (ok) {
    state.failCount = 0;
    els.conn.className = "status live";
    els.connLabel.textContent = "live";
  } else {
    state.failCount += 1;
    if (state.failCount >= 2) {
      els.conn.className = "status down";
      els.connLabel.textContent = "offline";
    }
  }
}

// --- Rendering ---------------------------------------------------------------

function progressHtml(doc) {
  if (doc.state === "FAILED") {
    return `
      <div class="progress failed" title="${escapeHtml(doc.error || "Failed")}">
        <div class="track">
          ${STEPS.map(() => '<span class="seg fail"></span>').join("")}
        </div>
        <span class="phase">Failed${
          doc.attempts ? ` · attempt ${doc.attempts}` : ""
        }</span>
      </div>`;
  }
  const done = STEP_PROGRESS[doc.state] ?? 0;
  const active = done < STEPS.length ? done : -1;
  const segs = STEPS.map((label, idx) => {
    let cls = "seg";
    if (idx < done) cls += " done";
    else if (idx === active) cls += " active";
    return `<span class="${cls}" title="${label}"></span>`;
  }).join("");
  const phase =
    doc.state === "COMPLETED"
      ? "Completed"
      : doc.state === "QUEUED"
      ? "Queued"
      : `${STEPS[active] || "Working"}…`;
  return `
    <div class="progress">
      <div class="track">${segs}</div>
      <span class="phase">${phase}</span>
    </div>`;
}

function actionsHtml(doc) {
  const buttons = [];
  if (doc.has_markdown) {
    buttons.push(
      `<button class="btn primary" data-act="preview" data-id="${doc.id}">Preview</button>`
    );
  }
  if (doc.state === "FAILED") {
    buttons.push(
      `<button class="btn" data-act="retry" data-id="${doc.id}">Retry</button>`
    );
  } else if (doc.state === "COMPLETED") {
    buttons.push(
      `<button class="btn" data-act="reprocess" data-id="${doc.id}">Reprocess</button>`
    );
  }
  buttons.push(
    `<button class="btn danger" data-act="delete" data-id="${doc.id}">Delete</button>`
  );
  return `<div class="row-actions">${buttons.join("")}</div>`;
}

function buildRow(doc) {
  const tr = document.createElement("tr");
  tr.dataset.id = doc.id;
  tr.innerHTML = `
    <td class="name"><span class="fname"></span></td>
    <td class="progress-col"></td>
    <td class="updated"></td>
    <td class="actions"></td>`;
  patchRow(tr, doc);
  return tr;
}

function patchRow(tr, doc) {
  const name = tr.querySelector(".fname");
  if (name.textContent !== doc.name) {
    name.textContent = doc.name;
    name.title = doc.name;
  }
  tr.querySelector(".progress-col").innerHTML = progressHtml(doc);
  tr.querySelector(".updated").textContent = fmtTime(doc.updated_at);
  // Do not repaint actions while an action is in flight for this row.
  if (!state.pending.has(doc.id)) {
    tr.querySelector(".actions").innerHTML = actionsHtml(doc);
  }
}

function render(documents) {
  state.docs = new Map(documents.map((d) => [d.id, d]));
  updateStats(documents);

  const tbody = els.rows;
  const empty = tbody.querySelector(".empty");
  if (!documents.length) {
    tbody.innerHTML =
      '<tr class="empty"><td colspan="4">No documents yet — upload one to begin.</td></tr>';
    return;
  }
  if (empty) empty.remove();

  const existing = new Map();
  tbody.querySelectorAll("tr[data-id]").forEach((tr) => {
    existing.set(tr.dataset.id, tr);
  });

  // Upsert in the new order; appendChild moves nodes, preserving identity.
  documents.forEach((doc) => {
    let tr = existing.get(doc.id);
    if (tr) {
      patchRow(tr, doc);
      existing.delete(doc.id);
    } else {
      tr = buildRow(doc);
    }
    tbody.appendChild(tr);
  });

  // Remove rows no longer present.
  existing.forEach((tr) => tr.remove());
}

function updateStats(documents) {
  let working = 0;
  let completed = 0;
  let failed = 0;
  for (const doc of documents) {
    if (doc.state === "COMPLETED") completed += 1;
    else if (doc.state === "FAILED") failed += 1;
    else if (WORKING_STATES.has(doc.state)) working += 1;
  }
  els.statTotal.textContent = documents.length;
  els.statWorking.textContent = working;
  els.statCompleted.textContent = completed;
  els.statFailed.textContent = failed;
}

// --- Data --------------------------------------------------------------------

async function refresh() {
  try {
    const res = await fetch("/api/documents");
    if (!res.ok) throw new Error("bad status");
    const data = await res.json();
    render(data.documents || []);
    setConnection(true);
  } catch (err) {
    setConnection(false);
  }
}

async function uploadFiles(files) {
  for (const file of files) {
    const form = new FormData();
    form.append("file", file);
    try {
      const res = await fetch("/api/upload", { method: "POST", body: form });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || "upload failed");
      toast(`Uploaded ${body.filename}`, "success");
    } catch (err) {
      toast(`${file.name}: ${err.message}`, "error");
    }
  }
  refresh();
}

async function manage(action, id, button) {
  state.pending.add(id);
  if (button) {
    button.disabled = true;
    button.classList.add("loading");
  }
  const method = action === "delete" ? "DELETE" : "POST";
  const url =
    action === "delete"
      ? `/api/documents/${id}`
      : `/api/documents/${id}/${action}`;
  try {
    const res = await fetch(url, { method });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || "action failed");
    const labels = { retry: "Re-queued", reprocess: "Re-queued", delete: "Deleted" };
    toast(labels[action] || "Done", "success");
    if (action === "delete" && state.activePreview && state.activePreview.id === id) {
      closePreview();
    }
  } catch (err) {
    toast(err.message, "error");
  } finally {
    state.pending.delete(id);
    await refresh();
  }
}

// --- Preview drawer ----------------------------------------------------------

function renderMetadata(text) {
  let data;
  try {
    data = JSON.parse(text);
  } catch (err) {
    return `<pre class="raw">${escapeHtml(text)}</pre>`;
  }
  const fields = [];
  const row = (label, value) =>
    `<div class="meta-row"><span class="meta-key">${escapeHtml(
      label
    )}</span><span class="meta-val">${value}</span></div>`;

  if (data.title) fields.push(row("Title", escapeHtml(data.title)));
  if (data.author) fields.push(row("Author", escapeHtml(data.author)));
  if (data.summary) fields.push(row("Summary", escapeHtml(data.summary)));
  if (Array.isArray(data.tags) && data.tags.length) {
    const chips = data.tags
      .map((t) => `<span class="chip">${escapeHtml(t)}</span>`)
      .join("");
    fields.push(row("Tags", `<div class="chips">${chips}</div>`));
  }
  const known = new Set(["title", "author", "summary", "tags"]);
  Object.keys(data)
    .filter((k) => !known.has(k))
    .forEach((k) => fields.push(row(k, escapeHtml(String(data[k])))));

  if (!fields.length) return `<pre class="raw">${escapeHtml(text)}</pre>`;
  return `<div class="meta">${fields.join("")}</div>`;
}

function renderMarkdownPreview(text) {
  const { frontMatter, body } = window.MarkdownRenderer.splitFrontMatter(text);
  let html = "";
  if (frontMatter.trim()) {
    html += `<details class="front-matter"><summary>Front matter</summary><pre>${escapeHtml(
      frontMatter.trim()
    )}</pre></details>`;
  }
  html += `<div class="markdown-body">${window.MarkdownRenderer.render(body)}</div>`;
  return html;
}

async function openPreview(id, tab = "markdown") {
  const doc = state.docs.get(id);
  state.activePreview = { id, tab };
  els.previewTitle.textContent = doc ? doc.name : id;
  els.tabs.forEach((t) => t.classList.toggle("active", t.dataset.tab === tab));

  els.dlMarkdown.href = `/download/markdown/${id}`;
  els.dlMetadata.href = `/download/metadata/${id}`;
  els.dlMetadata.style.display =
    doc && doc.has_metadata === false ? "none" : "";

  els.preview.hidden = false;
  els.preview.setAttribute("aria-hidden", "false");
  els.previewBody.innerHTML = '<div class="loading-text">Loading…</div>';

  try {
    const res = await fetch(`/api/documents/${id}/${tab}`);
    if (!res.ok) throw new Error("not available");
    const text = await res.text();
    els.previewBody.innerHTML =
      tab === "markdown" ? renderMarkdownPreview(text) : renderMetadata(text);
    els.previewBody.scrollTop = 0;
  } catch (err) {
    els.previewBody.innerHTML = `<div class="loading-text">Could not load ${tab}.</div>`;
  }
}

function closePreview() {
  els.preview.hidden = true;
  els.preview.setAttribute("aria-hidden", "true");
  state.activePreview = null;
}

// --- Confirm modal -----------------------------------------------------------

let modalResolver = null;

function confirmDialog({ title, text, confirmLabel }) {
  els.modalTitle.textContent = title;
  els.modalText.textContent = text;
  els.modalConfirm.textContent = confirmLabel || "Confirm";
  els.modal.hidden = false;
  els.modalConfirm.focus();
  return new Promise((resolve) => {
    modalResolver = resolve;
  });
}

function closeModal(result) {
  els.modal.hidden = true;
  if (modalResolver) {
    modalResolver(result);
    modalResolver = null;
  }
}

// --- Events ------------------------------------------------------------------

function bindEvents() {
  els.dropzone.addEventListener("click", () => els.fileInput.click());
  els.dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      els.fileInput.click();
    }
  });
  els.fileInput.addEventListener("change", (e) => {
    if (e.target.files.length) uploadFiles([...e.target.files]);
    e.target.value = "";
  });

  ["dragenter", "dragover"].forEach((evt) =>
    els.dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      els.dropzone.classList.add("dragover");
    })
  );
  ["dragleave", "drop"].forEach((evt) =>
    els.dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      els.dropzone.classList.remove("dragover");
    })
  );
  els.dropzone.addEventListener("drop", (e) => {
    if (e.dataTransfer.files.length) uploadFiles([...e.dataTransfer.files]);
  });

  els.rows.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const { act, id } = btn.dataset;
    if (act === "preview") {
      openPreview(id);
    } else if (act === "delete") {
      const ok = await confirmDialog({
        title: "Delete document?",
        text: "This removes the input file, its cleaned outputs, and its processing history. This cannot be undone.",
        confirmLabel: "Delete",
      });
      if (ok) manage("delete", id, btn);
    } else {
      manage(act, id, btn);
    }
  });

  els.previewClose.addEventListener("click", closePreview);
  els.tabs.forEach((tab) =>
    tab.addEventListener("click", () => {
      if (state.activePreview) openPreview(state.activePreview.id, tab.dataset.tab);
    })
  );

  els.modalConfirm.addEventListener("click", () => closeModal(true));
  els.modalCancel.addEventListener("click", () => closeModal(false));
  els.modal.addEventListener("click", (e) => {
    if (e.target === els.modal) closeModal(false);
  });

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!els.modal.hidden) closeModal(false);
    else if (!els.preview.hidden) closePreview();
  });
}

// --- Boot --------------------------------------------------------------------

bindEvents();
refresh();
setInterval(() => {
  if (!document.hidden) refresh();
}, POLL_MS);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});
