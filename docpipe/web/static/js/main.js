/*
 * Application entry point: state, polling, and event wiring.
 */

import { getHealth, listDocuments, manageDocument, uploadFile } from "./api.js";
import { el, toast } from "./dom.js";
import {
  isWorking,
  renderRows,
  renderSkeleton,
  renderStats,
} from "./documents.js";
import * as preview from "./preview.js";

const POLL_ACTIVE_MS = 2000;
const POLL_IDLE_MS = 10000;
const HEALTH_INTERVAL_MS = 30000;
const THEME_KEY = "doc-cleaner-theme";

const nodes = {
  rows: document.getElementById("rows"),
  banner: document.getElementById("banner"),
  health: document.getElementById("health"),
  search: document.getElementById("search"),
  dropzone: document.getElementById("dropzone"),
  fileInput: document.getElementById("file-input"),
  confirm: document.getElementById("confirm"),
  confirmTitle: document.getElementById("confirm-title"),
  confirmText: document.getElementById("confirm-text"),
  themeToggle: document.getElementById("theme-toggle"),
};

const state = {
  documents: [],
  pending: new Set(), // ids with an action in flight
  filter: "",
  pollTimer: null,
  offline: false,
};

// --- Theme -------------------------------------------------------------------

function applyTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}

function currentTheme() {
  if (document.documentElement.dataset.theme) {
    return document.documentElement.dataset.theme;
  }
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function initTheme() {
  try {
    const stored = localStorage.getItem(THEME_KEY);
    if (stored === "dark" || stored === "light") applyTheme(stored);
  } catch {
    /* Private browsing or blocked storage: the OS preference still applies. */
  }
  nodes.themeToggle.addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    applyTheme(next);
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {
      /* Not worth telling the user about. */
    }
  });
}

// --- Banner ------------------------------------------------------------------

function showBanner(tone, strong, detail) {
  nodes.banner.replaceChildren(
    el("span", {}, [
      el("strong", { text: `${strong} ` }),
      document.createTextNode(detail),
    ]),
  );
  nodes.banner.dataset.tone = tone;
  nodes.banner.hidden = false;
}

function hideBanner() {
  nodes.banner.hidden = true;
}

// --- Health ------------------------------------------------------------------

async function refreshHealth() {
  try {
    const health = await getHealth();
    nodes.health.dataset.status = health.status;
    nodes.health.textContent =
      health.status === "ok" ? health.model : "Model unavailable";
    nodes.health.title = health.ollama ?? `Using ${health.model}`;
    if (health.ollama) showBanner("warn", "Pipeline degraded.", health.ollama);
    else if (!state.offline) hideBanner();
  } catch {
    delete nodes.health.dataset.status;
    nodes.health.textContent = "Unknown";
  }
}

// --- Documents ---------------------------------------------------------------

function visibleDocuments() {
  if (!state.filter) return state.documents;
  const needle = state.filter.toLowerCase();
  return state.documents.filter((doc) =>
    `${doc.name} ${doc.key}`.toLowerCase().includes(needle),
  );
}

function paint() {
  renderStats(state.documents);
  renderRows(nodes.rows, visibleDocuments(), {
    pending: state.pending,
    selectedId: preview.activeId(),
    filtered: Boolean(state.filter),
  });
}

/** Fetch the list and repaint. Returns true if anything is still in flight. */
async function refresh() {
  try {
    state.documents = await listDocuments();
    if (state.offline) {
      state.offline = false;
      hideBanner();
      refreshHealth();
    }
    paint();
    return isWorking(state.documents);
  } catch (error) {
    // Silence here is what made the old UI look frozen when the backend died.
    state.offline = true;
    showBanner(
      "danger",
      "Cannot reach the server.",
      `${error.message}. Showing the last known state; retrying automatically.`,
    );
    return false;
  }
}

function schedulePoll(working) {
  clearTimeout(state.pollTimer);
  if (document.hidden) return;
  state.pollTimer = setTimeout(
    async () => schedulePoll(await refresh()),
    working || state.offline ? POLL_ACTIVE_MS : POLL_IDLE_MS,
  );
}

async function refreshNow() {
  schedulePoll(await refresh());
}

// --- Upload ------------------------------------------------------------------

async function upload(files) {
  for (const file of files) {
    try {
      const result = await uploadFile(file);
      toast(`Uploaded ${result.filename}`, "success");
    } catch (error) {
      toast(`${file.name}: ${error.message}`, "error");
    }
  }
  await refreshNow();
}

// --- Row actions -------------------------------------------------------------

const ACTION_LABELS = {
  retry: "Re-queued",
  reprocess: "Re-queued",
  delete: "Deleted",
};

async function runAction(action, id, button) {
  state.pending.add(id);
  if (button) button.disabled = true;
  try {
    await manageDocument(action, id);
    toast(ACTION_LABELS[action] ?? "Done", "success");
    if (action === "delete" && preview.activeId() === id) preview.close();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    state.pending.delete(id);
    await refreshNow();
  }
}

function confirmDelete() {
  return new Promise((resolve) => {
    const dialog = nodes.confirm;
    const finish = (result) => {
      dialog.close();
      resolve(result);
    };
    dialog.addEventListener("close", () => resolve(false), { once: true });
    document
      .getElementById("confirm-ok")
      .addEventListener("click", () => finish(true), { once: true });
    document
      .getElementById("confirm-cancel")
      .addEventListener("click", () => finish(false), { once: true });
    dialog.showModal();
  });
}

// --- Events ------------------------------------------------------------------

function bindEvents() {
  nodes.dropzone.addEventListener("click", () => nodes.fileInput.click());
  nodes.fileInput.addEventListener("change", (event) => {
    if (event.target.files.length) upload([...event.target.files]);
    event.target.value = "";
  });

  for (const type of ["dragenter", "dragover"]) {
    nodes.dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      nodes.dropzone.classList.add("is-dragover");
    });
  }
  for (const type of ["dragleave", "drop"]) {
    nodes.dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      nodes.dropzone.classList.remove("is-dragover");
    });
  }
  nodes.dropzone.addEventListener("drop", (event) => {
    if (event.dataTransfer.files.length) upload([...event.dataTransfer.files]);
  });

  nodes.search.addEventListener("input", (event) => {
    state.filter = event.target.value.trim();
    paint();
  });

  nodes.rows.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-act]");
    if (button) {
      const { act, id } = button.dataset;
      if (act === "preview") {
        openPreviewFor(id);
      } else if (act === "delete") {
        if (await confirmDelete()) runAction("delete", id, button);
      } else {
        runAction(act, id, button);
      }
      return;
    }
    const row = event.target.closest("tr[data-id]");
    if (row?.classList.contains("is-openable")) openPreviewFor(row.dataset.id);
  });

  preview.onClose(paint);

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) clearTimeout(state.pollTimer);
    else refreshNow();
  });
}

function openPreviewFor(id) {
  const doc = state.documents.find((item) => item.id === id);
  preview.open(id, doc?.name ?? id);
  paint(); // open() records the selection synchronously, before it fetches
}

// --- Boot --------------------------------------------------------------------

initTheme();
bindEvents();
renderSkeleton(nodes.rows);
refreshHealth();
setInterval(refreshHealth, HEALTH_INTERVAL_MS);
refreshNow();
