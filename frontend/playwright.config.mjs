import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  workers: 1,
  retries: 0,
  timeout: 150000,
  expect: { timeout: 15000 },
  use: {
    baseURL: process.env.VIBEEVAL_E2E_BASE_URL ?? 'http://127.0.0.1:5181',
    channel: 'chrome',
    actionTimeout: 5000,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    permissions: ['clipboard-read', 'clipboard-write'],
  },
});
