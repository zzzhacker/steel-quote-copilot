// Steel RFQ Copilot UI. No framework and no build step: DOM is built with h() so user text is never parsed as HTML.
const $ = (sel, root = document) => root.querySelector(sel);

const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : String(kid));
  return el;
};

const usd = (n) => n.toLocaleString("en-US", { style: "currency", currency: "USD" });
const human = (s) => s.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
const newId = () => Math.random().toString(36).slice(2) + Date.now().toString(36); // crypto.randomUUID needs https

const S = { thread: null, quote: null, draft: null, busy: false, samples: [], open: new Set() };

/* ---------- server-sent events over POST ---------- */
async function sse(url, body, handlers) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok || !res.body) throw new Error((await res.json().catch(() => ({}))).detail || `Server returned ${res.status}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf = (buf + decoder.decode(value, { stream: true })).replace(/\r\n/g, "\n");
    let end;
    while ((end = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, end);
      buf = buf.slice(end + 2);
      let event = "message";
      const data = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      }
      if (data.length) handlers[event]?.(JSON.parse(data.join("\n")));
    }
  }
}

/* ---------- pipeline ---------- */
const STEPS = {
  extract: ["Extract", "LLM reads the email"],
  revise: ["Revise", "LLM turns chat into a patch"],
  normalize: ["Normalize", "ontology rules"],
  resolve: ["Resolve", "MCP hybrid search"],
  validate: ["Validate", "live ERP check"],
  price: ["Price", "deterministic code"],
  review: ["Review", "a person approves"],
};
let pipe = [];

function pipeReset(first) {
  pipe = [first, "normalize", "resolve", "validate", "price", "review"].map((id) => ({ id, status: "pending", ms: 0, detail: "" }));
  pipe[0].status = "active";
  renderPipe();
}

function pipeStep({ node, ms, detail }) {
  const i = pipe.findIndex((s) => s.id === node);
  if (i < 0) return;
  Object.assign(pipe[i], { status: "done", detail: detail || pipe[i].detail, ms: pipe[i].ms + ms });
  for (let j = i + 1; j < pipe.length; j++) Object.assign(pipe[j], { status: "pending", ms: 0, detail: "" }); // a retry re-runs later steps
  if (pipe[i + 1] && pipe[i + 1].id !== "review") pipe[i + 1].status = "active";
  renderPipe();
}

function pipeReview(status, detail) {
  const review = pipe.find((s) => s.id === "review");
  if (review) Object.assign(review, { status, detail });
  renderPipe();
}

function renderPipe() {
  $("#pipeline").replaceChildren(...pipe.map((s) => h("li", { class: `step ${s.status}` },
    h("div", { class: "name" }, h("span", { class: "dot" }), STEPS[s.id][0]),
    h("div", { class: "sub" }, STEPS[s.id][1]),
    h("div", { class: "time" }, s.status === "done" || s.id === "review" ? [s.ms ? `${(s.ms / 1000).toFixed(1)} s` : "", s.detail].filter(Boolean).join(" · ") : s.status === "active" ? "working…" : ""))));
  const total = pipe.reduce((sum, s) => sum + s.ms, 0);
  $("#pipe-total").textContent = total ? `${(total / 1000).toFixed(1)} s total` : "";
}

/* ---------- chat thread ---------- */
function addMsg(role, ...body) {
  $("#thread-empty")?.remove();
  const el = h("div", { class: `msg ${role}` }, ...body);
  $("#thread").append(el);
  $("#thread").scrollTop = $("#thread").scrollHeight;
}

function describeChange(prev, next) {
  const before = new Map(prev.lines.map((p) => [p.line.norm.line_no, p]));
  const notes = [];
  for (const p of next.lines) {
    const n = p.line.norm.line_no, old = before.get(n);
    const sku = (x) => x.line.candidate?.default_code ?? "no match";
    if (!old) notes.push(`Line ${n} added: ${sku(p)}`);
    else if (sku(old) !== sku(p) || old.total !== p.total || old.line.status !== p.line.status)
      notes.push(`Line ${n}: ${sku(old)} → ${sku(p)} (${usd(old.total)} → ${usd(p.total)})`);
    before.delete(n);
  }
  for (const n of before.keys()) notes.push(`Line ${n} removed`);
  if (!notes.length) return h("span", {}, "The quote did not change. Try being more specific, like “make line 1 3/8”.");
  const delta = next.total - prev.total;
  return h("div", {}, `Updated. Total ${usd(next.total)} (${delta >= 0 ? "+" : "−"}${usd(Math.abs(delta))}).`,
    h("ul", {}, notes.map((t) => h("li", {}, t))));
}

/* ---------- quote ---------- */
const STATUS = { ok: "Ready", short: "Short on stock", needs_clarification: "Needs clarification" };

function lineRows(pl) {
  const r = pl.line, n = r.norm, isOpen = S.open.has(n.line_no);
  const toggle = () => { isOpen ? S.open.delete(n.line_no) : S.open.add(n.line_no); renderQuote(); };
  const main = h("tr", {
    class: `line${isOpen ? " open" : ""}`, tabindex: "0", role: "button", "aria-expanded": String(isOpen),
    onclick: toggle, onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } },
  },
    h("td", { class: "num" }, h("span", { class: "caret", "aria-hidden": "true" }, "▸"), n.line_no),
    h("td", { class: "req" }, n.raw_text),
    h("td", {}, r.candidate
      ? [h("div", { class: "sku" }, r.candidate.default_code), h("div", { class: "sub" }, r.candidate.name)]
      : [h("div", { class: "sku" }, "No match"), h("div", { class: "sub" }, "the agent abstained")]),
    h("td", {}, r.allocs.length
      ? r.allocs.map((a) => h("div", { class: "sub" }, `${a.qty}× ${a.is_remnant ? "remnant" : "full"} · ${a.heat}`))
      : h("span", { class: "sub" }, "—")),
    h("td", { class: "num" }, n.qty ?? "—"),
    h("td", {}, h("span", { class: `status ${r.status}` }, STATUS[r.status])),
    h("td", { class: "num" }, pl.total ? usd(pl.total) : "—"));
  if (!isOpen) return [main];

  const rows = Object.entries(pl.breakdown).filter(([, v]) => v);
  const detail = h("tr", { class: "detail" }, h("td", { colspan: "7" }, h("div", { class: "detail-grid" },
    h("div", {},
      h("h4", {}, "Checks"),
      h("ul", { class: "checks" }, r.checks.map((c) => h("li", {},
        h("span", { class: c.ok ? "v" : "x" }, c.ok ? "✓ " : "✗ "), human(c.name), c.detail ? h("span", { class: "sub" }, ` — ${c.detail}`) : ""))),
      r.allocs.length ? [h("h4", {}, "Stock allocation"), h("ul", { class: "checks" }, r.allocs.map((a) =>
        h("li", {}, `${a.qty}× heat `, h("span", { class: "mono" }, a.heat), a.is_remnant ? ` · remnant ${a.w_in}×${a.l_in} in` : " · full sheet", h("span", { class: "sub" }, ` · ${a.location}`))))] : "",
      r.alternatives.length ? [h("h4", {}, "Also considered"), h("ul", { class: "alts" }, r.alternatives.slice(0, 3).map((c) => h("li", { class: "mono" }, c.default_code)))] : ""),
    h("div", {},
      h("h4", {}, "Price breakdown"),
      rows.length ? h("div", { class: "kv" }, rows.map(([k, v]) => [h("span", {}, human(k)), h("span", { class: "num" }, usd(v))]),
        h("span", { class: "total" }, "Line total"), h("span", { class: "num total" }, usd(pl.total)))
        : h("p", { class: "sub" }, "Not priced until the line is resolved."),
      h("p", { class: "sub", style: "margin-top:8px" }, `${pl.weight_lb.toLocaleString("en-US", { maximumFractionDigits: 0 })} lb`)))));
  return [main, detail];
}

function renderQuote() {
  const box = $("#quote");
  const q = S.quote;
  if (!q) {
    box.replaceChildren(h("div", { class: "card empty" }, h("h3", {}, "No quote yet"),
      h("p", {}, "Pick a sample on the left and press Run quote. Each pipeline step lights up as it finishes.")));
    return;
  }
  const counts = { ok: 0, short: 0, needs_clarification: 0 };
  q.lines.forEach((p) => counts[p.line.status]++);
  const flagged = counts.short + counts.needs_clarification;
  const weight = q.lines.reduce((sum, p) => sum + p.weight_lb, 0);

  const approveArea = S.draft
    ? h("div", { class: "draft" },
        h("div", {}, h("strong", {}, `Draft ${S.draft.name} created in Odoo`),
          h("div", { class: "note" }, "An audit note with checks and models is in the order's chatter.")),
        h("div", { class: "row", style: "margin:0" },
          h("a", { class: "btn primary", href: S.draft.url, target: "_blank", rel: "noopener" }, "Open in Odoo"),
          h("button", { class: "btn", onclick: resetAll }, "New request")))
    : h("div", { class: "actions" },
        h("button", { class: "btn primary big", id: "approve", disabled: S.busy || !q.partner_id, onclick: approve }, "Approve and create draft in Odoo"),
        h("span", { class: "note" }, !q.partner_id ? "Customer not resolved: name them in the chat first."
          : flagged ? `${flagged} flagged line${flagged > 1 ? "s" : ""}: unmatched or $0 lines are left off, short lines are drafted for the stock on hand.`
          : "Only a draft quotation is written. A person confirms it in Odoo."));

  box.replaceChildren(h("div", { class: "card" },
    h("div", { class: "quote-head" },
      h("h3", {}, q.customer_name || "Customer not resolved"),
      q.tier ? h("span", { class: "tag" }, `Tier ${q.tier}`) : "",
      q.certs_required ? h("span", { class: "tag" }, "Certs required") : "",
      q.ship_by ? h("span", { class: "tag" }, `Ship by ${q.ship_by}`) : "",
      q.partner_id ? "" : h("span", { class: "tag bad" }, "Needs a customer")),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["#", "Requested", "Matched item", "Stock source", "Qty", "Status", "Total"].map((t, i) => h("th", { class: i === 0 || i >= 4 && t !== "Status" ? "num" : "" }, t)))),
      h("tbody", {}, q.lines.flatMap(lineRows)))),
    h("p", { class: "note", style: "margin-top:8px" }, "Click a line to see its checks, heat numbers and price breakdown."),
    h("div", { class: "totals" },
      h("div", { class: "stat" }, h("div", { class: "label" }, "Quote total"), h("div", { class: "value" }, usd(q.total)), h("div", { class: "hint" }, "computed by code, not the model")),
      h("div", { class: "stat" }, h("div", { class: "label" }, "Lines"), h("div", { class: "value" }, q.lines.length),
        h("div", { class: "hint" }, `${counts.ok} ready${counts.short ? ` · ${counts.short} short` : ""}${counts.needs_clarification ? ` · ${counts.needs_clarification} to clarify` : ""}`)),
      h("div", { class: "stat" }, h("div", { class: "label" }, "Weight"), h("div", { class: "value" }, `${weight.toLocaleString("en-US", { maximumFractionDigits: 0 })} lb`), h("div", { class: "hint" }, "from the ontology"))),
    approveArea));
}

/* ---------- actions ---------- */
function setBusy(busy) {
  S.busy = busy;
  const canChat = !busy && S.quote && !S.draft;
  $("#run").disabled = busy;
  $("#chat-input").disabled = !canChat;
  $("#chat-send").disabled = !canChat;
  document.querySelectorAll(".chip").forEach((c) => (c.disabled = !canChat));
  $("#run").textContent = busy ? "Running…" : "Run quote";
  const approveBtn = $("#approve");
  if (approveBtn) approveBtn.disabled = busy || !S.quote?.partner_id;
}

// fetch rejects with a TypeError when the server is unreachable or restarted mid-request
const explain = (e) => (e instanceof TypeError ? "Lost connection to the server (it may have restarted). Reload the page and run the request again." : e.message);

function showError(message) {
  const box = $("#alert");
  box.textContent = message;
  box.hidden = !message;
}

async function runStream(url, body, onResult) {
  showError("");
  setBusy(true);
  try {
    await sse(url, body, {
      step: pipeStep,
      result: (d) => { if (!d.quote) return showError("The agent returned no quote."); onResult(d.quote); },
      error: (d) => showError(d.message),
    });
  } catch (e) {
    showError(explain(e));
  } finally {
    pipe.forEach((s) => { if (s.status === "active") s.status = "pending"; });
    renderPipe();
    setBusy(false);
  }
}

async function startRun() {
  const email = $("#email").value.trim();
  if (!email || S.busy) return showError(email ? "" : "Paste an email or pick a sample first.");
  Object.assign(S, { thread: newId(), quote: null, draft: null });
  S.open.clear();
  $("#thread").replaceChildren(h("p", { class: "muted", id: "thread-empty" }, "Quote is running…"));
  renderQuote();
  pipeReset("extract");
  await runStream("/api/run", { thread: S.thread, email }, (quote) => {
    S.quote = quote;
    pipeReview("wait", "waiting for you");
    $("#thread-empty")?.remove();
    addMsg("bot", `Quote ready: ${quote.lines.length} line${quote.lines.length > 1 ? "s" : ""}, ${usd(quote.total)}. Change anything in plain English, or approve it.`);
    renderQuote();
  });
  renderQuote();
}

async function sendEdit(text) {
  text = text.trim();
  if (!text || S.busy || !S.quote || S.draft) return;
  addMsg("user", text);
  $("#chat-input").value = "";
  const prev = S.quote;
  pipeReset("revise");
  await runStream("/api/edit", { thread: S.thread, text }, (quote) => {
    S.quote = quote;
    pipeReview("wait", "waiting for you");
    addMsg("bot", describeChange(prev, quote));
  });
  renderQuote();
}

async function approve() {
  showError("");
  setBusy(true);
  try {
    const res = await fetch("/api/approve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ thread: S.thread }) });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Server returned ${res.status}`);
    S.draft = (await res.json()).draft;
    pipeReview("done", `draft ${S.draft.name}`);
    addMsg("bot", `Draft ${S.draft.name} created in Odoo.`);
  } catch (e) {
    showError(explain(e));
  }
  renderQuote();
  setBusy(false);
}

function resetAll() {
  Object.assign(S, { thread: null, quote: null, draft: null });
  S.open.clear();
  pipe = [];
  $("#pipeline").replaceChildren();
  $("#pipe-total").textContent = "";
  $("#thread").replaceChildren(h("p", { class: "muted", id: "thread-empty" }, "Run a request first, then change lines in plain English."));
  showError("");
  renderQuote();
  setBusy(false);
}

/* ---------- samples, chips, header ---------- */
async function initCopilot() {
  const [samples, info] = await Promise.all([fetch("/api/samples").then((r) => r.json()), fetch("/api/info").then((r) => r.json())]);
  S.samples = samples;
  const groups = new Map();
  samples.forEach((s) => groups.set(s.scenario, [...(groups.get(s.scenario) || []), s]));
  $("#sample").replaceChildren(...[...groups].map(([name, items]) =>
    h("optgroup", { label: name }, items.map((s) => h("option", { value: s.id }, s.label.length > 58 ? s.label.slice(0, 57) + "…" : s.label)))));
  const pick = () => {
    const s = S.samples.find((x) => x.id === $("#sample").value);
    $("#email").value = s.email.trim();
    $("#about").textContent = s.about;
  };
  $("#sample").addEventListener("change", pick);
  pick();

  const models = new Set(Object.values(info.models));
  $("#stack").replaceChildren(...["Odoo 19", "LangGraph", "MCP", `LLM: ${[...models].join(" + ")}`].map((t) => h("span", { class: "pill" }, t)));

  $("#chips").replaceChildren(...["make line 1 3/8", "set line 3 qty to 1", "remove line 2", "certs not required"].map((t) =>
    h("button", { class: "chip", type: "button", disabled: true, onclick: () => sendEdit(t) }, t)));
  $("#chat-form").addEventListener("submit", (e) => { e.preventDefault(); sendEdit($("#chat-input").value); });
  $("#run").addEventListener("click", startRun);
  $("#reset").addEventListener("click", () => { resetAll(); pick(); });
  $("#email").addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") startRun(); });
  renderQuote();
}

/* ---------- evals tab ---------- */
const CONFIGS = {
  lite: "Gemini flash-lite (baseline)", ungrounded: "Ungrounded (model writes SKU)", keyword: "Keyword search only",
  vector: "Vector search only", no_retry: "No validate retry", luna: "gpt-6-luna", terra: "gpt-6.1-terra", sol: "gpt-6.1-sol",
  routed: "Routed: luna + terra", routed_sol: "Routed: luna reads, sol picks", routed_rev: "Routed: sol reads, luna picks",
};
const METRICS = [
  { k: "line_f1", label: "Line F1", best: "max", fmt: (v) => v.toFixed(3) },
  { k: "sku_acc", label: "SKU accuracy", best: "max", fmt: (v) => v.toFixed(3) },
  { k: "price_err", label: "Price error", best: "min", fmt: (v) => v.toFixed(3) },
  { k: "cost_per_rfq", label: "Cost per RFQ", best: "min", fmt: (v) => `$${v.toFixed(5)}` },
  { k: "p50_latency_s", label: "p50 latency (s)", best: "min", fmt: (v) => v.toFixed(1) },
];
let evalsLoaded = false;

async function loadEvals() {
  if (evalsLoaded) return;
  evalsLoaded = true;
  const rows = await fetch("/api/evals").then((r) => r.json());
  const root = $("#tab-evals");
  if (!rows.length) return root.replaceChildren(h("div", { class: "card empty" }, h("h3", {}, "No results yet"), h("p", {}, "Run: uv run python -m evals.run --all")));
  const by = Object.fromEntries(rows.map((r) => [r.config, r]));
  const name = (c) => CONFIGS[c] || c;

  const tiles = [];
  if (by.lite && by.ungrounded) tiles.push(h("div", { class: "tile" }, h("div", { class: "label" }, "Grounding matters"),
    h("div", { class: "big" }, `${by.ungrounded.sku_acc.toFixed(2)} → ${by.lite.sku_acc.toFixed(2)}`), h("div", { class: "muted" }, "SKU accuracy when the model writes the SKU itself vs picks from ERP candidates")));
  const top = [...rows].sort((a, b) => b.line_f1 - a.line_f1)[0];
  tiles.push(h("div", { class: "tile" }, h("div", { class: "label" }, "Best accuracy"), h("div", { class: "big" }, top.line_f1.toFixed(3)), h("div", { class: "muted" }, `${name(top.config)} · line F1`)));
  const value = rows.filter((r) => r.line_f1 >= 0.93).sort((a, b) => a.cost_per_rfq - b.cost_per_rfq)[0];
  if (value) tiles.push(h("div", { class: "tile" }, h("div", { class: "label" }, "Best value at F1 ≥ 0.93"), h("div", { class: "big" }, `$${value.cost_per_rfq.toFixed(5)}`), h("div", { class: "muted" }, `${name(value.config)} · per RFQ`)));

  const chart = h("div", { class: "bars", id: "bars" });
  const seg = h("div", { class: "seg", role: "group", "aria-label": "Metric" });
  const draw = (m) => {
    const sorted = rows.filter((r) => r[m.k] != null).sort((a, b) => (m.best === "max" ? b[m.k] - a[m.k] : a[m.k] - b[m.k]));
    const max = Math.max(...sorted.map((r) => r[m.k])) || 1;
    chart.replaceChildren(...sorted.map((r, i) => h("div", { class: `bar${i === 0 ? " best" : ""}` },
      h("span", {}, name(r.config)), h("div", { class: "track" }, h("div", { class: "fill", style: `width:${Math.max(2, (r[m.k] / max) * 100)}%` })), h("span", { class: "val" }, m.fmt(r[m.k])))));
    seg.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.k === m.k));
  };
  METRICS.forEach((m) => seg.append(h("button", { type: "button", "data-k": m.k, onclick: () => draw(m) }, m.label)));
  draw(METRICS[0]);

  root.replaceChildren(h("div", { class: "tiles" }, tiles),
    h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Compare configurations"), h("span", { class: "muted" }, "best first; lower is better for error, cost and latency")), seg, chart,
      h("p", { class: "fine" }, "40 synthetic RFQs, one run per configuration, so differences under about 3 points are noise. Cost is list-price equivalent. Gemini ran on the free tier behind a rate limiter, so its latency is inflated.")));
}

/* ---------- how it works tab ---------- */
function loadHow() {
  const box = (title, sub, hot) => h("div", { class: `box${hot ? " hot" : ""}` }, h("b", {}, title), h("span", {}, sub));
  const layer = (tier, ...boxes) => h("div", { class: "layer" }, h("div", { class: "tier" }, tier), h("div", { class: "boxes" }, boxes));
  const arrow = (t) => h("div", { class: "arrow" }, `↓ ${t}`);
  $("#tab-how").replaceChildren(
    h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Architecture")),
      h("div", { class: "layers" },
        layer("Interface", box("This page", "streams each step live"), box("Eval harness", "40 RFQs, 10 configs")),
        arrow("start, edit, approve"),
        layer("Agent", box("LangGraph", "checkpointed, human review"), box("Extract", "LLM"), box("Resolve", "LLM picks from IDs", true), box("Validate", "retry once"), box("Price", "pure code")),
        arrow("MCP, streamable HTTP"),
        layer("Grounding", box("resolve_item", "BM25 + vector, RRF", true), box("resolve_customer", "name to partner"), box("find_stock", "remnant first, live"), box("validate_entities", "live check")),
        arrow("JSON-2 API and SQL"),
        layer("Systems", box("Odoo 19", "products, lots, quotes"), box("Postgres + pgvector", "hybrid index")),
        layer("Observability", box("Langfuse", "traces, datasets, scores")))),
    h("div", { class: "two" },
      h("div", { class: "card" }, h("h3", {}, "The model does"), h("ul", {}, ["Reads the messy email into lines", "Chooses among ERP candidate IDs, or abstains", "Turns a chat edit into a patch"].map((t) => h("li", {}, t)))),
      h("div", { class: "card" }, h("h3", {}, "Code does"), h("ul", {}, ["Normalizes grades, fractions, gauges and units", "Retrieves candidates and validates against live stock", "Weights, surcharges, cuts, certs, tiers and totals", "Writes only a draft, with an audit note"].map((t) => h("li", {}, t))))));
}

/* ---------- tabs ---------- */
document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((t) => { t.classList.toggle("on", t === tab); t.setAttribute("aria-selected", String(t === tab)); });
  for (const id of ["copilot", "evals", "how"]) $(`#tab-${id}`).hidden = id !== tab.dataset.tab;
  if (tab.dataset.tab === "evals") loadEvals();
  if (tab.dataset.tab === "how") loadHow();
}));

initCopilot().catch((e) => showError(`Could not load the app: ${e.message}`));
