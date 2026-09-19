# Security policy

## Threat model — read this before exposing the web UI

doc-cleaner is a **single-user tool meant to run on your own machine.** The web
interface has **no authentication and no authorization by design**, and it
exposes destructive operations: `DELETE /api/documents/{hash}` removes the input
file, both output artifacts and the ledger entry.

Consequences:

- The default bind address is `127.0.0.1`, and `docker-compose.yml` publishes
  the port on loopback only. **Keep it that way** unless you put an
  authenticating reverse proxy in front of it.
- Setting `DOCPIPE_WEB_HOST=0.0.0.0` and publishing the port makes uploading and
  deleting documents available to everyone who can reach the host.
- **Input documents are trusted.** They are parsed by Docling, which runs a
  substantial PDF/OCR stack. Do not point doc-cleaner at documents from
  untrusted sources on a machine you care about.

## What the project does defend against

- Cross-site writes: unsafe methods are rejected when the browser reports a
  cross-site origin, so a page you visit cannot drive your local pipeline.
- Stored XSS through document content: the bundled Markdown renderer escapes
  every character before applying a fixed set of transformations, and blocks
  non-`http(s)`/`mailto` URL schemes.
- Data exfiltration through previews: a `Content-Security-Policy` restricts
  images and every other subresource to the app itself, so a cleaned document
  containing a remote `<img>` cannot phone home.
- Path traversal: output paths are derived internally and checked for
  containment within the output directories.

## Reporting a vulnerability

Please open a [private security advisory](https://github.com/martinjanev/doc-cleaner/security/advisories/new)
rather than a public issue. Include what you did, what happened, and what you
expected. Expect a first response within a week.
