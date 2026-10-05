# Fixture del spike de Playwright

Fixture mínimo y desechable del spike `../playwright-behavioral-capabilities.md`. No es código productivo del behavioral verifier.

```bash
npm install && npx playwright install chromium
./run.sh <nombre> [args de playwright test...]   # escribe out/<nombre>/{playwright.json,stdout.txt,artifacts/}
```

Los tests (`tests/behavioral`) provocan PASS, fallo de aserción (`toHaveText`, `toBe`, `toEqual`, `toBeVisible`), timeout de selector, 404, flaky (falla el attempt 0 y pasa el 1), fallo repetido, skip y error de `beforeEach`. `tests/only` contiene un `test.only` (usar `-c playwright.only.config.ts`).
