import { test, expect } from '@playwright/test';
test('F-TOHAVETEXT', { tag: ['@behavioral', '@FAIL-TEXT-001'] }, async ({ page }) => {
  await page.goto('/login');
  await expect(page.locator('h1')).toHaveText('Dashboard');
});
test('F-TOBE', { tag: ['@behavioral', '@FAIL-TOBE-001'] }, async ({ page }) => {
  await page.goto('/login');
  const n = await page.locator('#count').innerText();
  expect(n).toBe('5');
});
test('F-TOEQUAL', { tag: ['@behavioral', '@FAIL-TOEQUAL-001'] }, async () => {
  expect({ a: 1, b: [1, 2] }).toEqual({ a: 1, b: [1, 3] });
});
test('F-TOBEVISIBLE', { tag: ['@behavioral', '@FAIL-VISIBLE-001'] }, async ({ page }) => {
  await page.goto('/login');
  await expect(page.locator('#nonexistent')).toBeVisible();
});
test('F-SELECTOR-TIMEOUT', { tag: ['@behavioral', '@FAIL-SELECTOR-001'] }, async ({ page }) => {
  await page.goto('/login');
  await page.locator('#nope').click();
});
test('F-HTTP-404', { tag: ['@behavioral', '@FAIL-HTTP-001'] }, async ({ page }) => {
  const r = await page.goto('/missing');
  expect(r?.status()).toBe(200);
});
