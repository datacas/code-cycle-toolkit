import { test, expect } from '@playwright/test';
import { base } from '../helpers/h';
test('R-FLAKY', { tag: ['@behavioral', '@RETRY-FLAKY-001'] }, async ({ page }, info) => {
  await page.goto(base);
  expect(info.retry, 'fails on first attempt only').toBeGreaterThan(0);
});
test('R-REPEATED', { tag: ['@behavioral', '@RETRY-REPEAT-001'] }, async ({ page }, info) => {
  await page.goto(base);
  expect(info.retry, 'always fails').toBeLessThan(0);
});
