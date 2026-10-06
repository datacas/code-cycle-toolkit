import { test, expect } from '@playwright/test';
test('M-SKIPPED', { tag: ['@behavioral', '@MISC-SKIP-001'] }, async () => {
  test.skip(true, 'skipped on purpose');
});
test.describe('setup group', () => {
  test.beforeEach(async () => { throw new Error('beforeEach boom'); });
  test('M-SETUP', { tag: ['@behavioral', '@MISC-SETUP-001'] }, async () => { expect(1).toBe(1); });
});
