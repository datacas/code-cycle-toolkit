import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests/behavioral',
  timeout: 15000,
  expect: { timeout: 1500 },
  use: { baseURL: 'http://127.0.0.1:4173', actionTimeout: 1500,
         screenshot: (process.env.PW_SHOT ?? 'only-on-failure') as any, video: (process.env.PW_VIDEO ?? 'off') as any, trace: (process.env.PW_TRACE ?? 'retain-on-first-failure') as any },
  webServer: { command: 'node server.mjs', url: 'http://127.0.0.1:4173/login', reuseExistingServer: true },
});
