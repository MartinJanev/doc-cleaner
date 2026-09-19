/*
 * The only module that talks to the server.
 *
 * Every call raises an Error carrying the server's `detail` message, so the UI
 * can show what actually went wrong instead of a generic failure.
 */

async function request(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body && body.detail) detail = body.detail;
    } catch {
      /* Not JSON; the status line is the best we have. */
    }
    throw new Error(detail);
  }
  return response;
}

export async function listDocuments() {
  const response = await request("/api/documents");
  const body = await response.json();
  return body.documents ?? [];
}

export async function getHealth() {
  const response = await request("/health");
  return response.json();
}

export async function uploadFile(file) {
  const form = new FormData();
  form.append("file", file);
  const response = await request("/api/upload", { method: "POST", body: form });
  return response.json();
}

export async function manageDocument(action, id) {
  const isDelete = action === "delete";
  const response = await request(
    isDelete ? `/api/documents/${id}` : `/api/documents/${id}/${action}`,
    { method: isDelete ? "DELETE" : "POST" },
  );
  return response.json();
}

export async function fetchPreview(id, kind) {
  const response = await request(`/api/documents/${id}/${kind}`);
  return response.text();
}

export function downloadUrl(id, kind) {
  return `/download/${kind}/${id}`;
}
