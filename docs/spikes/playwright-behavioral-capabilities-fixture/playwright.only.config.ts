import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests/only',
  timeout: 15000,
  expect: { timeout: 1500 },
  use: { baseURL: 'http://127.0.0.1:4173', actionTimeout: 1500,
         screenshot: 'only-on-failure', video: 'on-first-retry', trace: 'on-first-retry' },
  webServer: { command: 'node server.mjs', url: 'http://127.0.0.1:4173/login', reuseExistingServer: true },
});
