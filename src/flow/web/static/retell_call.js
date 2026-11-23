// Real voice agent in the browser: a Retell web call to this business's agent.
// The server starts the call with its own key (POST /api/demo/<slug>/voice-call) and returns
// a one-call access token, so no Retell key ever reaches this page.
import { RetellWebClient } from "/static/vendor/retell-sdk-3.0.3.js";

const cfg = window.FLOW_RETELL;
const $ = (id) => document.getElementById(id);
const btn = $("retell-btn"), status = $("retell-status"), talking = $("retell-talking");
const transcript = $("retell-transcript"), after = $("retell-after");
let client = null, live = false, failed = false;

function show(text) { status.textContent = text; }
function idle() {
  live = false; client = null; talking.hidden = true;
  btn.textContent = "Start voice call"; btn.classList.add("primary"); btn.disabled = false;
}

// Retell sends the whole transcript so far on every update: redraw it.
function draw(turns) {
  transcript.innerHTML = "";
  for (const t of turns || []) {
    if (!t.content) continue;
    const div = document.createElement("div");
    div.className = "bubble " + (t.role === "agent" ? "agent" : "caller");
    div.textContent = t.content;
    transcript.appendChild(div);
  }
  transcript.scrollTop = transcript.scrollHeight;
}

// After the call, ask the server how it went (Retell's record). Fills in the transcript if
// live updates didn't arrive and says why a call ended early.
async function review(callId) {
  try {
    const r = await fetch(`/api/demo/${encodeURIComponent(cfg.slug)}/voice-call/${encodeURIComponent(callId)}`, { credentials: "same-origin" });
    if (!r.ok) return;
    const info = await r.json();
    console.log("retell call", info.call_status, info.disconnection_reason);
    if (!transcript.querySelector(".bubble") && info.transcript) {
      draw(info.transcript.split("\n").filter(Boolean).map((l) => {
        const m = l.match(/^(Agent|User):\s*(.*)$/);
        return m ? { role: m[1] === "Agent" ? "agent" : "user", content: m[2] } : { role: "agent", content: l };
      }));
    }
    const normal = ["user_hangup", "agent_hangup", "call_transfer", "voicemail_reached"];
    if (info.disconnection_reason && !normal.includes(info.disconnection_reason)) {
      show("Call ended (" + info.disconnection_reason.replace(/_/g, " ") + ")");
    }
  } catch (_) {}
}

async function start() {
  btn.disabled = true; failed = false; after.textContent = "";
  show("Connecting… allow the microphone when asked");
  let tokens;
  try {
    const r = await fetch(`/api/demo/${encodeURIComponent(cfg.slug)}/voice-call`, {
      method: "POST", credentials: "same-origin",
    });
    tokens = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(tokens.detail || `HTTP ${r.status}`);
  } catch (err) {
    idle(); show("Couldn't start the call: " + (err.message || err)); return;
  }
  client = new RetellWebClient();
  client.on("call_started", () => {
    live = true; show("Connected · speak now (you can interrupt)");
    btn.textContent = "Hang up"; btn.classList.remove("primary"); btn.disabled = false;
  });
  client.on("update", (e) => draw(e.transcript));
  client.on("agent_start_talking", () => { talking.hidden = false; });
  client.on("agent_stop_talking", () => { talking.hidden = true; });
  client.on("call_ended", () => {
    const wasLive = live; idle();
    if (wasLive) setTimeout(() => review(tokens.call_id), 2500);
    if (!failed) {
      show("Call ended");
      if (wasLive) after.innerHTML = 'The call, lead and any booking appear on the <a href="/app">dashboard</a> within a minute (sign in as the business owner).';
    }
  });
  client.on("error", (err) => {
    failed = true; try { client && client.stopCall(); } catch (_) {}
    idle(); show("Call dropped: " + (err && err.message ? err.message : err));
  });
  try {
    transcript.innerHTML = "";
    await client.startCall({
      accessToken: tokens.access_token,
      transport: tokens.transport,
      url: tokens.url,
      iceServers: tokens.ice_servers,
      callId: tokens.call_id,
    });
  } catch (err) {
    failed = true; idle(); show("Couldn't connect: " + (err && err.message ? err.message : err));
  }
}

btn.addEventListener("click", () => {
  if (client) { btn.disabled = true; client.stopCall(); return; }
  start();
});
document.documentElement.dataset.retellReady = "1";
