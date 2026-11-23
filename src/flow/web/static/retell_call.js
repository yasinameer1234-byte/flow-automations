// Real voice agent in the browser: a Retell web call to this business's agent.
// The server starts the call with its own key (POST /api/demo/<slug>/voice-call) and returns
// a one-call access token, so no Retell key ever reaches this page.
import { RetellWebClient } from "/static/vendor/retell-sdk-3.0.3.js";

const cfg = window.FLOW_RETELL;
const $ = (id) => document.getElementById(id);
const btn = $("retell-btn"), status = $("retell-status"), talking = $("retell-talking");
const transcript = $("retell-transcript"), after = $("retell-after");
let client = null, live = false, failed = false, heard = false;

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
    live = true; heard = false; show("Connecting audio…");
    btn.textContent = "Hang up"; btn.classList.remove("primary"); btn.disabled = false;
  });
  const hear = () => { if (!heard) { heard = true; show("Connected · speak now (you can interrupt)"); } };
  client.on("update", (e) => { hear(); draw(e.transcript); });
  client.on("agent_start_talking", () => { hear(); talking.hidden = false; });
  client.on("agent_stop_talking", () => { talking.hidden = true; });
  client.on("call_ended", () => {
    const wasLive = live; idle();
    if (wasLive) setTimeout(() => review(tokens.call_id), 2500);
    if (!failed && wasLive && !heard) {
      show("Couldn't connect the audio. This network may block voice calls: try another network, or use the text demo below.");
    } else if (!failed) {
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
// Typing: the same Retell prompt and tools, as a text chat (no microphone needed).
let chatId = null, chatTurns = [];
const chatForm = $("chat-form"), chatSay = $("chat-say"), chatSend = $("chat-send");
chatForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const text = chatSay.value.trim();
  if (!text || client) return;
  chatSay.value = ""; chatSend.disabled = true;
  if (!chatTurns.length) transcript.innerHTML = "";
  chatTurns.push({ role: "user", content: text }); draw(chatTurns);
  show("The receptionist is typing…");
  try {
    const r = await fetch(`/api/demo/${encodeURIComponent(cfg.slug)}/agent-chat`, {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: text, chat_id: chatId }),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
    chatId = j.chat_id;
    for (const m of j.messages) if (m.role === "agent" && m.content) chatTurns.push(m);
    draw(chatTurns);
    if (j.ended) { chatId = null; chatTurns = []; show("Chat ended. Type again to start a new one."); }
    else show("Text chat with the AI receptionist");
  } catch (err) { show("Couldn't send: " + (err.message || err)); }
  chatSend.disabled = false; chatSay.focus();
});
document.documentElement.dataset.retellReady = "1";
