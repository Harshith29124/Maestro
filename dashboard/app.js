/* Maestro dashboard logic — drives one orchestration run live over WebSocket,
   falling back to the REST endpoint if WS is unavailable. No framework. */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const els = {
    task: $("task"), run: $("run"), sample: $("sample"), apikey: $("apikey"),
    status: $("status"), timeline: $("timeline"), answer: $("answer"),
    verdict: $("verdict"), totals: $("totals"), export: $("export"),
  };

  const SAMPLE =
    "A farmer has 17 sheep. All but 9 run away. How many are left? " +
    "Then explain the common mistake people make on this puzzle.";

  let lastLog = null;

  const ROLE_ORDER = ["conductor", "thinker", "worker", "verifier", "synthesizer"];

  function selectedMode() {
    const checked = document.querySelector('input[name="mode"]:checked');
    return checked ? checked.value : "conductor";
  }

  function setBusy(busy) {
    els.run.disabled = busy;
    els.sample.disabled = busy;
    els.run.textContent = busy ? "Running…" : "Run orchestration";
  }

  function reset() {
    els.timeline.innerHTML = "";
    els.answer.textContent = "";
    els.answer.classList.remove("muted");
    els.verdict.hidden = true;
    els.totals.textContent = "";
    els.export.disabled = true;
    lastLog = null;
  }

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  function nodeId(step) { return "node-" + step.step; }

  function renderStep(step) {
    let li = document.getElementById(nodeId(step));
    if (!li) {
      li = document.createElement("li");
      li.id = nodeId(step);
      li.className = "node";
      els.timeline.appendChild(li);
    }
    const tokens = step.tokens || { in: 0, out: 0 };
    const verdictBadge = step.verdict
      ? `<span class="badge ${step.verdict}">${esc(step.verdict)}</span>`
      : step.retry_triggered
        ? `<span class="badge retry">retry</span>`
        : "";
    const output = step.output || "";
    li.className = "node done";
    li.innerHTML = `
      <div class="node-head">
        <span class="node-role">${esc(step.role)}</span>
        <span class="node-model">${esc(step.model)}</span>
      </div>
      ${verdictBadge}
      ${step.routing_rationale ? `<p class="node-rationale">${esc(step.routing_rationale)}</p>` : ""}
      <button class="toggle" type="button">show contribution</button>
      <div class="node-output">${esc(output)}</div>
      <div class="node-meta">
        <span>in ${tokens.in} · out ${tokens.out} tok</span>
        <span>${step.latency_ms} ms</span>
        ${step.error ? `<span style="color:var(--accent)">${esc(step.error)}</span>` : ""}
      </div>`;
    const toggle = li.querySelector(".toggle");
    toggle.addEventListener("click", () => {
      li.classList.toggle("open");
      toggle.textContent = li.classList.contains("open") ? "hide contribution" : "show contribution";
    });
  }

  function finalize(log) {
    lastLog = log;
    els.answer.textContent = log.final_answer || "(no answer)";
    const vs = log.verification_status || "unverified";
    els.verdict.hidden = false;
    els.verdict.className = "verdict " + vs;
    els.verdict.textContent = vs;
    const t = log.totals || {};
    els.totals.textContent = `${t.calls || 0} calls · ${t.tokens || 0} tokens · ${t.wall_ms || 0} ms`;
    els.export.disabled = false;
  }

  function runViaWS(task, mode, key) {
    return new Promise((resolve, reject) => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${location.host}/ws/orchestrate`);
      let settled = false;
      ws.onopen = () => ws.send(JSON.stringify({ task, mode, api_key: key }));
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === "start") els.status.textContent = `running ${msg.mode}…`;
        else if (msg.type === "step") renderStep(msg.step);
        else if (msg.type === "complete") { finalize(msg.decision_log); settled = true; els.status.textContent = "done"; resolve(); ws.close(); }
        else if (msg.type === "error") { settled = true; reject(new Error(msg.detail)); ws.close(); }
      };
      ws.onerror = () => { if (!settled) reject(new Error("ws_error")); };
      ws.onclose = () => { if (!settled) reject(new Error("ws_closed")); };
    });
  }

  async function runViaREST(task, mode, key) {
    els.status.textContent = "running (REST)…";
    const headers = { "Content-Type": "application/json" };
    if (key) headers["X-API-Key"] = key;
    const res = await fetch("/orchestrate", {
      method: "POST", headers, body: JSON.stringify({ task, mode }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    (data.decision_log.steps || []).forEach(renderStep);
    finalize(data.decision_log);
    els.status.textContent = "done";
  }

  async function go(task) {
    if (!task.trim()) { els.status.textContent = "enter a task first"; return; }
    reset();
    setBusy(true);
    const mode = selectedMode();
    const key = els.apikey.value.trim();
    try {
      try {
        await runViaWS(task, mode, key);
      } catch (wsErr) {
        // Fall back to REST for any WS failure that isn't an explicit server error.
        if (/rate limit|invalid api key|too long|empty/i.test(wsErr.message)) throw wsErr;
        await runViaREST(task, mode, key);
      }
    } catch (err) {
      els.status.textContent = "error: " + err.message;
    } finally {
      setBusy(false);
    }
  }

  els.run.addEventListener("click", () => go(els.task.value));
  els.sample.addEventListener("click", () => { els.task.value = SAMPLE; go(SAMPLE); });
  els.export.addEventListener("click", () => {
    if (!lastLog) return;
    const blob = new Blob([JSON.stringify(lastLog, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `maestro-${lastLog.run_id || "run"}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  });
})();
