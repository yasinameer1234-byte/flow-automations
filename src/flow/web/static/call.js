(() => {
  const slug = window.FLOW_TENANT;
  const $ = (id) => document.getElementById(id);
  const transcript = $("transcript"), say = $("say"), send = $("send"), mic = $("mic");
  const callBtn = $("call-btn"), status = $("status"), voice = $("voice"), after = $("after");
  let callId = null, busy = false;
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  let rec = null;

  function bubble(kind, text) {
    const div = document.createElement("div");
    div.className = "bubble " + kind;
    div.textContent = text;
    div.dataset.speaker = kind;
    transcript.appendChild(div);
    transcript.scrollTop = transcript.scrollHeight;
  }
  function speak(text) {
    if (!voice.checked || !("speechSynthesis" in window)) return;
    try { speechSynthesis.cancel(); speechSynthesis.speak(new SpeechSynthesisUtterance(text)); } catch (e) {}
  }
  function setLive(live) {
    say.disabled = send.disabled = !live;
    mic.disabled = !live || !SR;
    callBtn.textContent = live ? "Hang up" : "Start call";
    callBtn.classList.toggle("primary", !live);
    status.textContent = live ? "Connected · AI assistant" : "Not connected";
    if (live) say.focus();
  }
  async function post(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  }
  async function finish() {
    if (!callId) return;
    const id = callId; callId = null; setLive(false);
    try {
      const res = await post(`/api/demo/${slug}/calls/${id}/end`);
      bubble("system", `Call ended · outcome: ${res.outcome} · ${res.billed_minutes} min`);
      after.innerHTML = 'The call, lead and booking now appear on the <a href="/app">dashboard</a> (sign in as the business owner).';
    } catch (e) { bubble("system", "Could not end call: " + e.message); }
  }
  async function start() {
    transcript.innerHTML = ""; after.textContent = "";
    try {
      const res = await post(`/api/demo/${slug}/calls`);
      callId = res.call_id; transcript.dataset.callId = callId;
      bubble("agent", res.reply); speak(res.reply);
      if (res.ended) { await finish(); return; }
      setLive(true);
    } catch (e) { bubble("system", "Could not connect: " + e.message); }
  }
  async function turn(text) {
    if (!callId || busy || !text.trim()) return;
    busy = true; bubble("caller", text); say.value = "";
    try {
      const res = await post(`/api/demo/${slug}/calls/${callId}/turn`, { text });
      bubble("agent", res.reply); speak(res.reply);
      if (res.ended) {
        if (res.transferred_to) bubble("system", `Transferring to ${res.transferred_to} (simulated)`);
        await finish();
      }
    } catch (e) { bubble("system", "Error: " + e.message); }
    busy = false;
  }
  callBtn.addEventListener("click", () => (callId ? finish() : start()));
  $("say-form").addEventListener("submit", (e) => { e.preventDefault(); turn(say.value); });
  $("suggest").addEventListener("click", (e) => {
    if (e.target.tagName === "BUTTON") { if (!callId) return; turn(e.target.textContent); }
  });
  if (SR) {
    mic.addEventListener("click", () => {
      rec = new SR(); rec.lang = "en-CA"; rec.interimResults = false;
      status.textContent = "Listening…";
      rec.onresult = (ev) => turn(ev.results[0][0].transcript);
      rec.onend = () => { if (callId) status.textContent = "Connected · AI assistant"; };
      try { speechSynthesis.cancel(); } catch (e) {}
      rec.start();
    });
  }
})();
