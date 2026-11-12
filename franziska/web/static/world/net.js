// The server, for the world page: the same JSON API and NDJSON chat stream as the classic UI (app.js).
export async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
  });
  if (res.status === 401) { location.reload(); throw new Error("not signed in"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

// POST (JSON, or a FormData with a file) and read NDJSON events as they arrive: status lines (with the route at
// work), then the reply.
export async function stream(path, body, onEvent) {
  const form = body instanceof FormData;
  const res = await fetch(path, { method: "POST", headers: form ? {} : { "Content-Type": "application/json" }, body: form ? body : JSON.stringify(body) });
  if (res.status === 401) { location.reload(); return; }
  if (!res.ok) { onEvent({ type: "reply", text: (await res.json().catch(() => ({}))).detail || res.statusText, stopped: "error" }); return; }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line) onEvent(JSON.parse(line));
    }
  }
}

export const store = {  // per-device conveniences only; the page works without them
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};

const esc = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// The agents write light markdown: **bold**, `code`, [text](url). Everything else stays as typed.
export const rich = (s) => esc(s)
  .replace(/^(\s*)[*-]\s+/gm, "$1• ")
  .replace(/\n{3,}/g, "\n\n")
  .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
  .replace(/`([^`\n]+)`/g, "<code>$1</code>")
  .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+|\/api\/reports\/[\w.-]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

export const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid);
  return el;
};

// "📅 Life" → { icon: "📅", name: "Life" }
export function splitLabel(label) {
  const m = /^(\p{Extended_Pictographic}️?)\s*(.*)$/u.exec(label || "");
  return m ? { icon: m[1], name: m[2] } : { icon: "◆", name: label || "" };
}
