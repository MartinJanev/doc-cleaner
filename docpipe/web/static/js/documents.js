/*
 * Renders the document table and the counters above it.
 *
 * Rows are reconciled rather than rebuilt: the list is polled, and replacing
 * the tbody wholesale would drop focus and hover on whatever the user was
 * pointing at mid-poll.
 */

import { clear, el, formatTime } from "./dom.js";

export const STEPS = ["Extract", "Refine", "Save"];

const STEPS_DONE = {
  QUEUED: 0,
  PENDING: 0,
  EXTRACTED: 1,
  RESTRUCTURED: 2,
  COMPLETED: 3,
};

export const WORKING_STATES = new Set([
  "QUEUED",
  "PENDING",
  "EXTRACTED",
  "RESTRUCTURED",
]);

export function isWorking(documents) {
  return documents.some((doc) => WORKING_STATES.has(doc.state));
}

function progressCell(doc) {
  const track = el("div", { class: "track" });
  const failed = doc.state === "FAILED";
  const done = STEPS_DONE[doc.state] ?? 0;
  const active = done < STEPS.length ? done : -1;

  STEPS.forEach((label, index) => {
    let cls = "seg";
    if (failed) cls += " is-failed";
    else if (index < done) cls += " is-done";
    else if (index === active) cls += " is-active";
    track.append(el("span", { class: cls, title: label }));
  });

  let phase;
  if (failed) {
    phase = doc.attempts ? `Failed · attempt ${doc.attempts}` : "Failed";
  } else if (doc.state === "COMPLETED") {
    phase = "Completed";
  } else if (doc.state === "QUEUED") {
    phase = "Queued";
  } else {
    phase = `${STEPS[active] ?? "Working"}…`;
  }

  return el("div", { class: "progress", title: failed ? doc.error || "" : "" }, [
    track,
    el("span", { class: failed ? "phase is-failed" : "phase", text: phase }),
  ]);
}

function actionsCell(doc) {
  const buttons = [];
  if (doc.has_markdown) {
    buttons.push(
      el("button", {
        class: "btn",
        type: "button",
        text: "Preview",
        "data-variant": "primary",
        dataset: { act: "preview", id: doc.id },
      }),
    );
  }
  if (doc.state === "FAILED") {
    buttons.push(
      el("button", {
        class: "btn",
        type: "button",
        text: "Retry",
        dataset: { act: "retry", id: doc.id },
      }),
    );
  } else if (doc.state === "COMPLETED") {
    buttons.push(
      el("button", {
        class: "btn",
        type: "button",
        text: "Reprocess",
        dataset: { act: "reprocess", id: doc.id },
      }),
    );
  }
  buttons.push(
    el("button", {
      class: "btn",
      type: "button",
      text: "Delete",
      "data-variant": "danger",
      dataset: { act: "delete", id: doc.id },
    }),
  );
  return buttons;
}

function buildRow(doc) {
  const row = el("tr", { dataset: { id: doc.id } }, [
    el("td", { class: "cell-name" }),
    el("td"),
    el("td", { class: "cell-time" }),
    el("td", { class: "cell-actions" }),
  ]);
  patchRow(row, doc, new Set());
  return row;
}

function patchRow(row, doc, pending) {
  const [nameCell, progressTd, timeCell, actionsTd] = row.children;

  // The folder is only worth showing when it disambiguates two same-named files.
  const folder = doc.key.includes("/")
    ? doc.key.slice(0, doc.key.lastIndexOf("/"))
    : null;
  if (nameCell.dataset.name !== doc.name || nameCell.dataset.folder !== folder) {
    nameCell.dataset.name = doc.name;
    if (folder) nameCell.dataset.folder = folder;
    else delete nameCell.dataset.folder;
    clear(nameCell);
    nameCell.append(document.createTextNode(doc.name));
    if (folder) nameCell.append(el("small", { text: folder }));
    nameCell.title = doc.name;
  }

  clear(progressTd).append(progressCell(doc));
  timeCell.textContent = formatTime(doc.updated_at);

  // Leave the buttons alone while one of them has an action in flight,
  // otherwise a poll would replace the button mid-click.
  if (!pending.has(doc.id)) {
    clear(actionsTd).append(...actionsCell(doc));
  }
  row.classList.toggle("is-openable", Boolean(doc.has_markdown));
}

export function renderRows(tbody, documents, { pending, selectedId, filtered }) {
  if (!documents.length) {
    clear(tbody).append(
      el("tr", { class: "empty-row" }, [
        el("td", { colspan: "4" }, [
          el("strong", {
            text: filtered ? "No matching documents" : "No documents yet",
          }),
          document.createTextNode(
            filtered
              ? "Try a different search term."
              : "Upload one above, or drop a file into data/input.",
          ),
        ]),
      ]),
    );
    return;
  }

  const existing = new Map();
  for (const row of tbody.querySelectorAll("tr[data-id]")) {
    existing.set(row.dataset.id, row);
  }
  tbody.querySelector(".empty-row")?.remove();

  for (const doc of documents) {
    let row = existing.get(doc.id);
    if (row) {
      patchRow(row, doc, pending);
      existing.delete(doc.id);
    } else {
      row = buildRow(doc);
    }
    row.classList.toggle("is-selected", doc.id === selectedId);
    tbody.append(row); // append moves an existing node, preserving identity
  }

  for (const row of existing.values()) row.remove();
}

export function renderSkeleton(tbody) {
  clear(tbody).append(
    el("tr", { class: "empty-row" }, [
      el("td", { colspan: "4" }, [
        el("div", { class: "skeleton" }),
        el("div", { class: "skeleton" }),
        el("div", { class: "skeleton" }),
      ]),
    ]),
  );
}

export function renderStats(documents) {
  let working = 0;
  let completed = 0;
  let failed = 0;
  for (const doc of documents) {
    if (doc.state === "COMPLETED") completed += 1;
    else if (doc.state === "FAILED") failed += 1;
    else if (WORKING_STATES.has(doc.state)) working += 1;
  }
  document.getElementById("stat-total").textContent = documents.length;
  document.getElementById("stat-working").textContent = working;
  document.getElementById("stat-completed").textContent = completed;
  document.getElementById("stat-failed").textContent = failed;
}
