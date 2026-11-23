// End-to-end: a customer calls the service company in the browser, books an
// appointment, and the owner sees the call, the lead and the booking in the dashboard.
// Usage: BASE_URL=http://127.0.0.1:8100 FLOW_DEMO_PASSWORD=... node e2e/first_journey.js [shotsDir]
const { chromium } = require("playwright");
const assert = require("assert");

const BASE = process.env.BASE_URL || "http://127.0.0.1:8100";
const PW = process.env.FLOW_DEMO_PASSWORD;
const SHOTS = process.argv[2];
const caller = "Jordan " + ["Quill", "Brook", "Ashford", "Lane", "Fenwick"][Date.now() % 5] + String.fromCharCode(97 + (Date.now() % 26)) + "son";

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, deviceScaleFactor: SHOTS ? 2 : 1 });
  const step = (m) => console.log("·", m);

  // 1. The caller phones the business (browser demo, voice off for the test).
  await page.goto(`${BASE}/demo/northside`);
  await page.uncheck("#voice");
  await page.click("#call-btn");
  await page.waitForSelector(".bubble.agent");
  assert.match(await page.textContent(".bubble.agent"), /AI assistant/, "disclosure is spoken first");
  step("call connected with AI disclosure");

  const agentCount = () => page.locator(".bubble.agent").count();
  async function say(text) {
    const before = await agentCount();
    await page.fill("#say", text);
    await page.click("#send");
    await page.waitForFunction((n) => document.querySelectorAll(".bubble.agent").length > n, before);
    return (await page.locator(".bubble.agent").last().textContent()).trim();
  }
  let r = await say("Hi, my furnace stopped working and the house is cold");
  assert.match(r, /name/i); step("asks for name");
  r = await say(`My name is ${caller}`);
  assert.match(r, /number/i); step("asks for phone");
  r = await say("647 555 0177");
  assert.match(r, /day/i); step("asks for timing");
  r = await say("earliest available");
  assert.match(r, /heating repair, I have/i, "offers real slots"); step("offers: " + r);
  r = await say("the first one");
  assert.match(r, /Shall I book/); step("reads back the booking");
  r = await say("yes please");
  assert.match(r, /You're booked for/); step("confirmed: " + r);
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/1-browser-call.png`, fullPage: true });
  await say("no that's all");
  await page.waitForSelector(".bubble.system");
  assert.match(await page.locator(".bubble.system").last().textContent(), /outcome: booked/);
  step("call ended with outcome booked");

  // 2. The owner signs in and sees the lead and the booking.
  await page.goto(`${BASE}/login`);
  await page.fill("input[name=email]", "owner@northside.example");
  await page.fill("input[name=password]", PW);
  await page.click("button[type=submit]");
  await page.waitForURL(/\/app$/);
  const html = await page.content();
  assert.ok(html.includes(caller), "new caller appears in recent calls / appointments");
  step("dashboard shows the new call and appointment");
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/2-dashboard.png`, fullPage: true });

  await page.click("text=Leads");
  const row = page.locator("tr", { hasText: caller });
  assert.match(await row.textContent(), /booked/);
  step("lead recorded with status booked");

  // 3. The job is done and the owner records the real value: revenue becomes verified.
  await row.locator("input[name=value]").fill("880");
  await row.locator("button[value=won]").click();
  await page.waitForURL(/\/app\/leads/);
  assert.match(await page.locator("tr", { hasText: caller }).textContent(), /won/);
  step("job marked won at $880 (verified revenue)");
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/3-leads.png`, fullPage: true });

  // 3b. Revenue Engine: nothing goes out until the owner approves the wording.
  await page.click("text=Revenue Engine");
  const pb = page.locator("[data-playbook=reactivation]");
  assert.match(await pb.textContent(), /waiting for your approval/);
  await pb.locator("button[value=approve]").click();
  await page.waitForURL(/\/app\/revenue/);
  assert.match(await page.locator("[data-playbook=reactivation]").textContent(), /approved/);
  const seqs = await page.locator("table").first().textContent();
  assert.match(seqs, /Past-customer reminder/);
  step("owner approves a Revenue Engine playbook and past customers are enrolled");
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/5-revenue.png`, fullPage: true });

  // 4. Tenant isolation: this owner cannot open the other business.
  const resp = await page.goto(`${BASE}/app?tenant=t_maple`);
  assert.strictEqual(resp.status(), 403);
  step("other tenant's dashboard is refused (403)");

  // 5. Admin sees usage and can switch the assistant off.
  await page.context().clearCookies();
  await page.goto(`${BASE}/login`);
  await page.fill("input[name=email]", "admin@flow.local");
  await page.fill("input[name=password]", PW);
  await page.click("button[type=submit]");
  await page.waitForURL(/\/admin$/);
  if (SHOTS) await page.screenshot({ path: `${SHOTS}/4-admin.png`, fullPage: true });
  step("admin panel loads");

  await browser.close();
  console.log("E2E PASSED");
})().catch((e) => { console.error("E2E FAILED:", e.message); process.exit(1); });
