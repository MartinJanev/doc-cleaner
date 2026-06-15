"use strict";

/*
 * Tiny, dependency-free Markdown renderer.
 *
 * Security model: every input character is HTML-escaped FIRST, then a known
 * subset of Markdown is transformed into a fixed set of tags. Raw HTML in the
 * source can never reach the DOM, so output is safe to assign via innerHTML.
 * Covers: headings, bold/italic, inline + fenced code, links, images, ordered
 * and unordered lists, blockquotes, horizontal rules, tables, and paragraphs.
 */

(function (global) {
  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // Only allow safe, non-script URL schemes (and relative/anchor links).
  function safeUrl(rawUrl) {
    const url = rawUrl.trim();
    if (/^(https?:|mailto:|#|\/|\.\/|\.\.\/)/i.test(url)) return url;
    if (/^[^:]+$/.test(url)) return url; // bare path, no scheme
    return "#";
  }

  function splitFrontMatter(text) {
    const match = /^\uFEFF?---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(text);
    if (!match) return { frontMatter: "", body: text };
    return { frontMatter: match[1], body: text.slice(match[0].length) };
  }

  // Inline transforms run on already-escaped text.
  function renderInline(text) {
    let out = text;

    // Inline code first so its contents are not further transformed.
    const codeSpans = [];
    out = out.replace(/`([^`]+)`/g, function (_m, code) {
      codeSpans.push(code);
      return "\u0000" + (codeSpans.length - 1) + "\u0000";
    });

    // Images: ![alt](src)
    out = out.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, function (_m, alt, src) {
      return '<img src="' + safeUrl(src) + '" alt="' + alt + '" />';
    });

    // Links: [text](url)
    out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (_m, label, url) {
      return (
        '<a href="' +
        safeUrl(url) +
        '" target="_blank" rel="noopener noreferrer">' +
        label +
        "</a>"
      );
    });

    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/__([^_]+)__/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    out = out.replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>");
    out = out.replace(/~~([^~]+)~~/g, "<del>$1</del>");

    // Restore inline code placeholders.
    out = out.replace(/\u0000(\d+)\u0000/g, function (_m, idx) {
      return "<code>" + codeSpans[Number(idx)] + "</code>";
    });

    return out;
  }

  function renderTable(rows) {
    const splitRow = (row) =>
      row
        .replace(/^\s*\|/, "")
        .replace(/\|\s*$/, "")
        .split("|")
        .map((cell) => cell.trim());

    const header = splitRow(rows[0]);
    const body = rows.slice(2).map(splitRow);

    let html = "<table><thead><tr>";
    header.forEach((cell) => (html += "<th>" + renderInline(cell) + "</th>"));
    html += "</tr></thead><tbody>";
    body.forEach((cols) => {
      html += "<tr>";
      cols.forEach((cell) => (html += "<td>" + renderInline(cell) + "</td>"));
      html += "</tr>";
    });
    html += "</tbody></table>";
    return html;
  }

  function isTableSeparator(line) {
    return /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(line) && line.includes("-");
  }

  function renderMarkdown(text) {
    const escaped = escapeHtml(text).replace(/\r\n/g, "\n");
    const lines = escaped.split("\n");
    const html = [];
    let i = 0;

    const flushParagraph = (buffer) => {
      if (buffer.length) {
        html.push("<p>" + renderInline(buffer.join(" ")) + "</p>");
        buffer.length = 0;
      }
    };

    const paragraph = [];

    while (i < lines.length) {
      const line = lines[i];

      // Fenced code block
      const fence = /^\s*```(.*)$/.exec(line);
      if (fence) {
        flushParagraph(paragraph);
        const code = [];
        i++;
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
          code.push(lines[i]);
          i++;
        }
        i++; // skip closing fence
        html.push("<pre><code>" + code.join("\n") + "</code></pre>");
        continue;
      }

      // Blank line
      if (/^\s*$/.test(line)) {
        flushParagraph(paragraph);
        i++;
        continue;
      }

      // Horizontal rule
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
        flushParagraph(paragraph);
        html.push("<hr />");
        i++;
        continue;
      }

      // Heading
      const heading = /^(#{1,6})\s+(.*)$/.exec(line);
      if (heading) {
        flushParagraph(paragraph);
        const level = heading[1].length;
        html.push(
          "<h" + level + ">" + renderInline(heading[2].trim()) + "</h" + level + ">"
        );
        i++;
        continue;
      }

      // Table (header + separator + rows)
      if (
        line.includes("|") &&
        i + 1 < lines.length &&
        isTableSeparator(lines[i + 1])
      ) {
        flushParagraph(paragraph);
        const tableRows = [line, lines[i + 1]];
        i += 2;
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
          tableRows.push(lines[i]);
          i++;
        }
        html.push(renderTable(tableRows));
        continue;
      }

      // Blockquote (note: ">" is already escaped to "&gt;" at this point)
      if (/^\s*&gt;\s?/.test(line)) {
        flushParagraph(paragraph);
        const quote = [];
        while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])) {
          quote.push(lines[i].replace(/^\s*&gt;\s?/, ""));
          i++;
        }
        html.push(
          "<blockquote><p>" + renderInline(quote.join(" ")) + "</p></blockquote>"
        );
        continue;
      }

      // Lists (ordered / unordered)
      if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
        flushParagraph(paragraph);
        const ordered = /^\s*\d+\.\s+/.test(line);
        const items = [];
        const itemRe = ordered ? /^\s*\d+\.\s+(.*)$/ : /^\s*[-*+]\s+(.*)$/;
        while (i < lines.length && itemRe.test(lines[i])) {
          items.push(renderInline(itemRe.exec(lines[i])[1]));
          i++;
        }
        const tag = ordered ? "ol" : "ul";
        html.push(
          "<" + tag + ">" + items.map((it) => "<li>" + it + "</li>").join("") + "</" + tag + ">"
        );
        continue;
      }

      // Default: accumulate into a paragraph
      paragraph.push(line.trim());
      i++;
    }

    flushParagraph(paragraph);
    return html.join("\n");
  }

  global.MarkdownRenderer = {
    render: renderMarkdown,
    splitFrontMatter: splitFrontMatter,
    escapeHtml: escapeHtml,
  };
})(window);
