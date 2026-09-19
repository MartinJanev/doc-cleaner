/*
 * Small DOM helpers.
 *
 * `el` exists so nothing in this app builds markup by concatenating strings.
 * Text always goes through textContent, which makes escaping a property of the
 * construction rather than something each call site has to remember.
 */

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") node.className = value;
    else if (name === "text") node.textContent = value;
    else if (name === "dataset") Object.assign(node.dataset, value);
    else node.setAttribute(name, value === true ? "" : value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child);
  }
  return node;
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

/** Relative time, falling back to a locale date beyond a day. */
export function formatTime(iso) {
  if (!iso) return "—";
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return "—";
  const seconds = (Date.now() - then.getTime()) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return then.toLocaleDateString();
}

let toastHost = null;

export function toast(message, tone = "") {
  toastHost ??= document.getElementById("toasts");
  const node = el("div", { class: "toast", text: message });
  if (tone) node.dataset.tone = tone;
  toastHost.append(node);
  setTimeout(() => {
    node.classList.add("is-leaving");
    setTimeout(() => node.remove(), 220);
  }, 4000);
}

/**
 * Keep Tab inside `container` while it is open, and restore focus on close.
 * The preview drawer is not a native <dialog> (the page behind it stays usable
 * on a wide screen), so this has to be done by hand.
 */
export function trapFocus(container) {
  const previous = document.activeElement;
  const selector =
    'a[href], button:not(:disabled), input, select, textarea, [tabindex]:not([tabindex="-1"])';

  function onKeydown(event) {
    if (event.key !== "Tab") return;
    const items = [...container.querySelectorAll(selector)].filter(
      (node) => node.offsetParent !== null,
    );
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  container.addEventListener("keydown", onKeydown);
  return function release() {
    container.removeEventListener("keydown", onKeydown);
    if (previous instanceof HTMLElement && document.contains(previous)) {
      previous.focus();
    }
  };
}
