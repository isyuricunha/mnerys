const path = require('node:path');
const os = require('node:os');
const { defineConfig } = require('@playwright/test');

// Runs real Gateway HTTP handlers, never app:app or production services.
const port = Number(process.env.MNERYS_NOTES_HTTP_E2E_PORT || 7015);
const root = path.resolve(__dirname, '..', '..', '..');
const python = process.env.MNERYS_NOTES_E2E_PYTHON || 'python3';

module.exports = defineConfig({
  testDir: __dirname,
  testMatch: 'integrated-browser.spec.js',
  outputDir: path.join(os.homedir(), 'chatgpt', 'mnerys', 'tests', 'gateway-notes-http'),
  timeout: 45_000,
  expect: { timeout: 8_000 },
  workers: 1,
  fullyParallel: false,
  reporter: 'line',
  use: {
    baseURL: 'http://127.0.0.1:' + port,
    browserName: 'chromium',
    headless: true,
    serviceWorkers: 'block',
    viewport: { width: 1440, height: 900 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: JSON.stringify(python) + ' tests/e2e/gateway-notes/gateway_fixture.py --port ' + port,
    cwd: root,
    url: 'http://127.0.0.1:' + port + '/__fixture/health',
    timeout: 30_000,
    reuseExistingServer: false,
    // Do not forward the developer shell's provider/API secrets to even a
    // test-only process. The fixture needs no credentials or external URLs.
    env: {
      PATH: process.env.PATH || '',
      HOME: process.env.HOME || '',
      LANG: process.env.LANG || 'C.UTF-8',
      PYTHONPATH: root,
      PYTHONDONTWRITEBYTECODE: '1',
      MNERYS_GATEWAY_NOTES_TEST_ONLY: '1',
    },
  },
});
