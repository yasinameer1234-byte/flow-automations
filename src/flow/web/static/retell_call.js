// Real voice agent in the browser: a Retell web call to this business's agent.
// Loaded only when FLOW_RETELL_PUBLIC_KEY and FLOW_RETELL_AGENT_<SLUG> are configured.
import { RetellClient } from "/static/vendor/retell-sdk-3.0.3.js";

const cfg = window.FLOW_RETELL;
const btn = document.getElementById("retell-btn");
const status = document.getElementById("retell-status");
const client = new RetellClient({ key: cfg.public_key });
let call = null, failed = false;

function show(text) { status.textContent = text; }
function idle() { call = null; btn.textContent = "Start voice call"; btn.disabled = false; }

btn.addEventListener("click", async () => {
  if (call) { btn.disabled = true; await call.end(); idle(); show("Call ended"); return; }
  btn.disabled = true;
  failed = false;
  show("Connecting… allow the microphone when asked");
  call = client.createWebCall({
    agent_id: cfg.agent_id,
    hooks: {
      onStatus: (s) => { if (s !== "ended") show(s === "live" ? "Connected · speak now" : s); },
      onEnd: () => { idle(); if (!failed) show("Call ended. It shows on the dashboard in a minute."); },
      onError: (err) => {
        failed = true; idle();
        show("Couldn't connect: " + (err && err.message ? err.message : err));
      },
    },
  });
  btn.textContent = "Hang up";
  btn.disabled = false;
  try {
    await call.ready;
  } catch (err) {
    failed = true; idle();
    show("Couldn't connect: " + (err && err.message ? err.message : err));
  }
});
document.documentElement.dataset.retellReady = "1";
