/*
 * The preview drawer.
 *
 * Markdown goes through the bundled renderer, which escapes before it
 * transforms; metadata is built as DOM nodes. Nothing here interpolates
 * document content into an HTML string.
 */

import { downloadUrl, fetchPreview } from "./api.js";
import { clear, el, trapFocus } from "./dom.js";
import { renderMarkdown, splitFrontMatter } from "./markdown.js";

const drawer = document.getElementById("drawer");
const title = document.getElementById("drawer-title");
const body = document.getElementById("drawer-body");
const tabs = [...drawer.querySelectorAll(".tab")];
const linkMarkdown = document.getElementById("download-markdown");
const linkMetadata = document.getElementById("download-metadata");

let active = null; // { id, tab }
let releaseFocus = null;
let onCloseCallback = () => {};

export function activeId() {
  return active?.id ?? null;
}

export function onClose(callback) {
  onCloseCallback = callback;
}

function metadataView(text) {
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    return el("pre", { class: "raw", text });
  }

  const row = (label, value) =>
    el("div", { class: "meta-row" }, [
      el("span", { class: "meta-key", text: label }),
      el("span", { class: "meta-val" }, [value]),
    ]);

  const rows = [];
  if (data.title) rows.push(row("Title", document.createTextNode(data.title)));
  if (data.author) rows.push(row("Author", document.createTextNode(data.author)));
  if (data.summary) {
    rows.push(row("Summary", document.createTextNode(data.summary)));
  }
  if (Array.isArray(data.tags) && data.tags.length) {
    const chips = el(
      "div",
      { class: "chips" },
      data.tags.map((tag) => el("span", { class: "chip", text: String(tag) })),
    );
    rows.push(row("Tags", chips));
  }

  const known = new Set(["title", "author", "summary", "tags"]);
  for (const [key, value] of Object.entries(data)) {
    if (known.has(key)) continue;
    rows.push(row(key, document.createTextNode(String(value))));
  }

  if (!rows.length) return el("pre", { class: "raw", text });
  return el("div", { class: "meta" }, rows);
}

function markdownView(text) {
  const { frontMatter, body: content } = splitFrontMatter(text);
  const nodes = [];
  if (frontMatter.trim()) {
    nodes.push(
      el("details", { class: "front-matter" }, [
        el("summary", { text: "Front matter" }),
        el("pre", { text: frontMatter.trim() }),
      ]),
    );
  }
  const rendered = el("div", { class: "markdown-body" });
  // Safe by construction: renderMarkdown escapes every character before
  // emitting its fixed tag set. See js/markdown.js.
  rendered.innerHTML = renderMarkdown(content);
  nodes.push(rendered);
  return nodes;
}

function selectTab(name) {
  for (const tab of tabs) {
    const selected = tab.dataset.tab === name;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  }
  body.setAttribute("aria-labelledby", `tab-${name}`);
}

export async function open(id, name, tab = "markdown") {
  const isFirstOpen = drawer.hidden;
  active = { id, tab };
  title.textContent = name || id;
  selectTab(tab);

  linkMarkdown.href = downloadUrl(id, "markdown");
  linkMetadata.href = downloadUrl(id, "metadata");

  drawer.hidden = false;
  if (isFirstOpen) {
    releaseFocus = trapFocus(drawer);
    document.getElementById("drawer-close").focus();
  }

  clear(body).append(
    el("div", { class: "skeleton" }),
    el("div", { class: "skeleton" }),
    el("div", { class: "skeleton" }),
  );

  try {
    const text = await fetchPreview(id, tab);
    if (active?.id !== id || active?.tab !== tab) return; // superseded
    clear(body).append(
      ...(tab === "markdown" ? markdownView(text) : [metadataView(text)]),
    );
    body.scrollTop = 0;
  } catch (error) {
    if (active?.id !== id) return;
    clear(body).append(
      el("p", { class: "placeholder", text: `Could not load ${tab}: ${error.message}` }),
    );
  }
}

export function close() {
  if (drawer.hidden) return;
  drawer.hidden = true;
  active = null;
  releaseFocus?.();
  releaseFocus = null;
  onCloseCallback();
}

drawer.addEventListener("click", (event) => {
  const tab = event.target.closest(".tab");
  if (tab && active) open(active.id, title.textContent, tab.dataset.tab);
});

document.getElementById("drawer-close").addEventListener("click", close);

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !drawer.hidden) close();
});
