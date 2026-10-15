const $ = (s) => document.querySelector(s);
const STARTERS = [
  "Which hubs in the Midwest are most exposed to winter disruption?",
  "Compare Miami and Houston in terms of hurricane and flood exposure.",
  "What percentage of days in Denver last year had snowfall?",
  "Why is the Dallas hub's weather disruption risk high?",
];
const TIER_CLASS = { "Very High": "t-vh", High: "t-h", Moderate: "t-m", Low: "t-l" };

function store(key, val) {
  try { if (val === undefined) return localStorage.getItem(key); val === null ? localStorage.removeItem(key) : localStorage.setItem(key, val); }
  catch { return null; }
}
let sessionId = store("sessionId");

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const md = (s) => (window.marked ? marked.parse(s) : `<p>${esc(s).replace(/\n/g, "<br>")}</p>`);
const list = (items) => `<ul>${items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>`;

function chips(container, questions) {
  questions.forEach((q) => {
    const b = document.createElement("button");
    b.className = "chip"; b.type = "button"; b.textContent = q;
    b.onclick = () => send(q);
    container.appendChild(b);
  });
}

function addMsg(cls, html) {
  const div = document.createElement("div");
  div.className = `msg ${cls}`;
  div.innerHTML = html;
  $("#messages").appendChild(div);
  div.scrollIntoView({ behavior: "smooth", block: "end" });
  return div;
}

function renderAnswer(r) {
  const section = (title, items) => (items && items.length ? `<details><summary>${title} (${items.length})</summary>${list(items)}</details>` : "");
  const tools = r.tool_trace.filter((t) => t.tool);
  const trace = r.tool_trace.map((t) => t.llm_call
    ? `◆ Claude call → ${t.stop_reason}  ${t.ms}ms`
    : `    ${t.is_error ? "✗" : "✓"} ${t.tool}(${JSON.stringify(t.input)})  ${t.ms}ms`).join("\n");
  const u = r.usage || {};
  const usageLine = u.llm_calls
    ? `\n\ntokens: ${u.input_tokens} uncached in · ${u.cache_read_input_tokens} cache read · ` +
      `${u.cache_creation_input_tokens} cache write · ${u.output_tokens} out`
    : "";
  const html = `
    ${md(r.answer)}
    <div class="badges">
      <span class="badge conf-${r.confidence}" title="${esc(r.confidence_reason)}">confidence: ${r.confidence}</span>
      <span class="badge">${esc(r.question_type.replace("_", " "))}</span>
      <span class="badge">${(r.latency_ms / 1000).toFixed(1)}s · ${tools.length} tool calls</span>
      ${r.validation_warnings.length ? `<span class="badge warn">${r.validation_warnings.length} validation warning(s)</span>` : ""}
    </div>
    ${section("Key drivers", r.key_drivers)}
    ${section("Assumptions", r.assumptions)}
    ${section("Caveats & uncertainty", r.caveats)}
    ${section("Data sources", r.data_sources)}
    ${section("Validation warnings", r.validation_warnings)}
    <details><summary>Agent trace</summary><div class="trace">${esc(trace + usageLine)}</div></details>
    <details><summary>Raw JSON</summary><div class="trace">${esc(JSON.stringify(r, null, 2))}</div></details>
    <div class="chips"></div>`;
  const div = addMsg("agent", html);
  chips(div.querySelector(".chips:last-child"), r.suggested_followups || []);
}

async function send(text) {
  text = (text || $("#input").value).trim();
  if (!text) return;
  $("#input").value = "";
  $("#send").disabled = true;
  addMsg("user", esc(text));
  const pending = addMsg("agent thinking", "Analyzing… (calling tools and scoring model)");
  try {
    const headers = { "Content-Type": "application/json" };
    const token = store("accessToken");
    if (token) headers["X-Access-Token"] = token;
    const res = await fetch("/api/chat", { method: "POST", headers, body: JSON.stringify({ message: text, session_id: sessionId }) });
    if (res.status === 401) {
      const t = prompt("This demo is protected. Enter the access token:");
      if (t) { store("accessToken", t); pending.remove(); $("#send").disabled = false; return send(text); }
      throw new Error("Access token required");
    }
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    const r = await res.json();
    sessionId = r.session_id; store("sessionId", sessionId);
    renderAnswer(r);   // render first: if it throws, the catch below can still show the error in `pending`
    pending.remove();
  } catch (e) {
    pending.className = "msg agent"; pending.textContent = `Error: ${e.message}`;
  } finally {
    $("#send").disabled = false; $("#input").focus();
  }
}

async function loadLeaderboard() {
  const hazard = $("#lb-hazard").value;
  const res = await fetch(`/api/scores?hazard=${hazard}`);
  const data = await res.json();
  $("#lb-list").innerHTML = data.ranking.map((r) => `
    <li title="${esc(r.tier)}" data-q="Why is the ${esc(r.name)}'s ${hazard === "overall" ? "weather disruption" : hazard.replace("_", " ")} risk ${r.tier.toLowerCase()}?">
      <span class="lb-rank">${r.rank}</span>
      <span><span class="lb-name">${esc(r.name.replace(" Hub", ""))}</span> <span class="muted small">${esc(r.region)}</span>
        <div class="lb-bar ${TIER_CLASS[r.tier]}" style="width:${r.score}%"></div></span>
      <span class="lb-score">${r.score.toFixed(0)}</span>
    </li>`).join("");
  document.querySelectorAll("#lb-list li").forEach((li) => (li.onclick = () => send(li.dataset.q)));
  $("#lb-meta").textContent = `Weights v${data.meta.weights_version} · climatology ${data.meta.climatology_window} · data fetched ${data.meta.data_fetched_at.slice(0, 10)}`;
}

$("#composer").onsubmit = (e) => { e.preventDefault(); send(); };
$("#lb-hazard").onchange = loadLeaderboard;
$("#new-chat").onclick = () => { sessionId = null; store("sessionId", null); location.reload(); };
chips($("#starter-chips"), STARTERS);
loadLeaderboard();
fetch("/health").then((r) => r.json()).then((h) => ($("#model").textContent = h.model));
