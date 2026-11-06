const path = require('node:path');
const os = require('node:os');
const { defineConfig } = require('@playwright/test');

// Lightweight fixture serves only public static assets from this checkout.
// Do NOT start app:app: doing so can initialize real auxiliary services even
// when the primary SQLite DATABASE_URL is redirected to a test database.
const port = Number(process.env.MNERYS_NOTES_E2E_PORT || 7014);
const repositoryRoot = path.resolve(__dirname, '..', '..', '..');
const python = process.env.MNERYS_NOTES_E2E_PYTHON || 'python3';

module.exports = defineConfig({
  testDir: __dirname,
  testMatch: 'browser-ui.spec.js',
  outputDir: path.join(os.homedir(), 'chatgpt', 'mnerys', 'tests', 'gateway-notes'),
  timeout: 30_000,
  expect: { timeout: 6_000 },
  workers: 1,
  fullyParallel: false,
  reporter: 'line',
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    browserName: 'chromium',
    headless: true,
    serviceWorkers: 'block',
    viewport: { width: 1440, height: 900 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: `${JSON.stringify(python)} tests/e2e/gateway-notes/serve.py --port ${port}`,
    cwd: repositoryRoot,
    url: `http://127.0.0.1:${port}/login`,
    timeout: 20_000,
    reuseExistingServer: false,
  },
});
