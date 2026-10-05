import { test, expect } from '@playwright/test';
test.only('O-ONLY', { tag: ['@behavioral', '@ONLY-001'] }, async () => { expect(1).toBe(1); });
test('O-OTHER', { tag: ['@behavioral', '@ONLY-002'] }, async () => { expect(1).toBe(1); });
