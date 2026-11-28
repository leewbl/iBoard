import { createRequire } from 'node:module';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

function displayText(text, limit) {
  return text.replace(/\s+/gu, ' ').trim().slice(0, limit);
}

async function observe(variant, filename, screenshotPath) {
  const requireFromImage = createRequire('/app/package.json');
  const { chromium } = requireFromImage('playwright');
  const target = pathToFileURL(join('/workspace', filename));
  const browser = await chromium.launch({
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    headless: true,
    timeout: 12_000,
  });
  let observation;
  try {
    const page = await browser.newPage({ viewport: { width: 1200, height: 800 } });
    page.setDefaultTimeout(8_000);
    let scriptError;
    page.on('pageerror', (error) => { scriptError ??= error; });
    await page.goto(target.href, { waitUntil: 'load' });

    const button = page.locator(`[data-variant="${variant}"]`);
    await button.first().waitFor({ state: 'visible' });
    if (await button.count() !== 1) {
      throw new Error(`expected exactly one control for variant ${variant}`);
    }
    const label = displayText(
      await button.getAttribute('aria-label') || await button.innerText() || variant,
      120,
    );
    await button.click();

    const result = page.locator(`[data-active-variant="${variant}"]`);
    await result.waitFor({ state: 'visible' });
    const current = new URL(page.url());
    if (current.protocol !== target.protocol || current.pathname !== target.pathname) {
      throw new Error('variant switch left its local prototype document');
    }
    const text = displayText(await result.innerText(), 700);
    if (!text) throw new Error(`variant ${variant} has no visible result text`);
    await page.screenshot({ path: screenshotPath, fullPage: true, animations: 'disabled' });
    if (scriptError) throw new Error(`Prototype page error. ${scriptError.message.slice(0, 300)}`);
    observation = `Clicked ${label || variant}. The page showed ${text}`;
  } finally {
    await browser.close();
  }
  return observation;
}

async function main() {
  const [variant, filename = 'prototype.html', ...extra] = process.argv.slice(2);
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(variant ?? '') ||
      !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.html?$/.test(filename) || extra.length) {
    throw new Error('expected a variant ID and an optional local HTML filename');
  }
  const screenshotPath = process.env.WORKBENCH_SCREENSHOT_PATH;
  if (!screenshotPath) throw new Error('WORKBENCH_SCREENSHOT_PATH was not supplied');
  const observation = await observe(variant, filename, screenshotPath);
  process.stdout.write(`${JSON.stringify({ kind: 'iboard-prototype', variant, observation, surface: 'visual' })}\n`);
}

main().catch((error) => {
  process.stderr.write(`Prototype visual driver stopped. ${String(error.message).slice(0, 500)}\n`);
  process.exitCode = 1;
});
