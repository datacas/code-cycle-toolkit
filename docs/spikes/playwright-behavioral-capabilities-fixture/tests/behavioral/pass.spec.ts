import { test, expect } from '@playwright/test';
test('login page renders', { tag: ['@behavioral', '@AUTH-LOGIN-001'],
  annotation: { type: 'covers', description: 'issue:151' } }, async ({ page }) => {
  await page.goto('/login');
  await expect(page.locator('h1')).toHaveText('Login');
});
test('login page renders (variant)', { tag: ['@behavioral', '@AUTH-LOGIN-0010'] }, async ({ page }) => {
  await page.goto('/login');
  await expect(page.locator('h1')).toBeVisible();
});
