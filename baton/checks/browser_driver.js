// Observes; never judges. Usage: node browser_driver.js <spec.json> <observations.json>
// `playwright` must resolve from the working directory (run it where the app's node_modules are).
'use strict';
const fs = require('fs');
const path = require('path');

let chromium;
try {
  ({ chromium } = require('playwright'));
} catch (e) {
  console.error('playwright is not installed here: ' + e.message);
  process.exit(3);
}

const spec = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

async function observe(browser, job) {
  const context = await browser.newContext(job.storage_state ? { storageState: job.storage_state } : {});
  const page = await context.newPage();
  const obs = { id: job.id, error: null, spinner_stuck: false, console_errors: [], requests: [],
                final_url: null, screenshot: null };
  page.on('console', (m) => { if (m.type() === 'error') obs.console_errors.push(m.text()); });
  page.on('pageerror', (e) => obs.console_errors.push('pageerror: ' + e.message));
  page.on('response', (r) => {
    const q = r.request();
    obs.requests.push({ url: q.url(), method: q.method(), status: r.status(), resource_type: q.resourceType() });
  });
  page.on('requestfailed', (q) => {
    obs.requests.push({ url: q.url(), method: q.method(), status: 0, resource_type: q.resourceType(),
                        failure: (q.failure() || {}).errorText || 'failed' });
  });
  try {
    await page.goto(job.url, { waitUntil: 'domcontentloaded', timeout: spec.nav_timeout_ms });
    await page.waitForLoadState('networkidle', { timeout: spec.nav_timeout_ms }).catch(() => {});
    if (spec.spinner_selector) {
      try {
        await page.waitForSelector(spec.spinner_selector, { state: 'hidden', timeout: spec.spinner_timeout_ms });
      } catch (e) {
        obs.spinner_stuck = true;
      }
    }
    obs.final_url = page.url();
    fs.mkdirSync(path.dirname(job.screenshot), { recursive: true });
    await page.screenshot({ path: job.screenshot, fullPage: true });
    obs.screenshot = job.screenshot;
    if (job.reference) {
      const ref = await context.newPage();
      try {
        await ref.goto(job.reference.url, { waitUntil: 'networkidle', timeout: spec.nav_timeout_ms });
        await ref.screenshot({ path: job.reference.screenshot, fullPage: true });
      } catch (e) {
        obs.reference_error = String(e.message || e);
      }
      await ref.close();
    }
    if (job.invalid_submit) obs.invalid_submit = await submitInvalid(page, job.invalid_submit);
  } catch (e) {
    obs.error = String(e.message || e).split('\n')[0];
  }
  await context.close();
  return obs;
}

async function submitInvalid(page, s) {
  const out = { message: null, status: null, error: null };
  let last = null;
  page.on('response', (r) => {
    const q = r.request();
    if (['fetch', 'xhr'].includes(q.resourceType()) && q.method() !== 'GET') last = r.status();
  });
  try {
    for (const step of s.steps || []) {
      if (step.fill !== undefined) await page.fill(step.fill, step.value || '', { timeout: 5000 });
      else if (step.click !== undefined) await page.click(step.click, { timeout: 5000 });
      else if (step.press !== undefined) await page.keyboard.press(step.press);
    }
    await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {});
    out.status = last;
    const sel = s.message_selector;
    if (sel) {
      // wait for a message with text in it: the element may exist, empty, before the response arrives
      const loc = page.locator(sel).filter({ hasText: /\S/ }).first();
      out.message = await loc.innerText({ timeout: s.wait_ms || 5000 }).catch(() => null);
    }
  } catch (e) {
    out.error = String(e.message || e).split('\n')[0];
  }
  return out;
}

(async () => {
  const browser = await chromium.launch(spec.launch || {});
  const observations = [];
  for (const job of spec.jobs) observations.push(await observe(browser, job));
  await browser.close();
  fs.writeFileSync(process.argv[3], JSON.stringify({ observations }, null, 2));
})().catch((e) => {
  console.error(String(e && e.stack || e));
  process.exit(1);
});
