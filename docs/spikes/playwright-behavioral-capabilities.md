# Spike: capacidades de Playwright para behavioral verification

Issue 1 de `docs/behavioral-verification-design.md`. Todo lo que se marca CONFIRMED o REJECTED se **ejecutó** en este spike; lo que no se pudo ejecutar se marca PARTIAL o "no probado".

**SPIKE VERDICT: GO WITH CHANGES.** Ningún hallazgo invalida la arquitectura. Hay correcciones concretas al diseño antes del Issue 2 (sección "Required design corrections"); las dos más importantes son el modo de trace por defecto y que `test.only` sin `--forbid-only` descarta tests en silencio.

## Environment

```text
Node:        v22.23.2
Playwright:  @playwright/test 1.63.0 (latest estable en npm el 2026-10-05; next = 1.64.0-alpha)
Browser:     Chromium 153.0.8010.12 (revisión 1243, headless shell)
OS:          Linux 6.18.33.2-microsoft-standard-WSL2 (WSL2)
Fixture:     docs/spikes/playwright-behavioral-capabilities-fixture/ (servidor Node mínimo en 127.0.0.1:4173)
Nota:        la primera ejecución en frío tardó 2,3 min; las siguientes, ~1 s. Una sola versión probada.
```

## Capability matrix

| # | Assumption | Result | Exact command | Observed behavior | Design impact |
|---|---|---|---|---|---|
| 1 | El reporter JSON se escribe a ruta conocida sin redirigir stdout | CONFIRMED | `PLAYWRIGHT_JSON_OUTPUT_NAME=out/r/playwright.json npx playwright test --reporter=line,json` | Escribe el fichero y crea los directorios intermedios. `PLAYWRIGHT_JSON_OUTPUT_DIR` no tuvo efecto. Con `--reporter=json` solo, el JSON va a stdout | `raw/playwright.json` es viable sin tocar stdout |
| 2 | Varios reporters a la vez (`json` + `line` + `html`) | CONFIRMED | `--reporter=line,json,html` con `PLAYWRIGHT_HTML_OPEN=never PLAYWRIGHT_HTML_REPORT=<dir>` | Funciona por CLI y por config (`reporter: [['line'],['json',{outputFile}],['html',{open:'never',outputFolder}]]`). **`--reporter` por CLI sustituye por completo los reporters de la config** | El toolkit puede imponer `line,json` por CLI sin editar la config del proyecto; el HTML del proyecto se pierde salvo que se incluya |
| 3 | `retries=1`: fallo → pass produce `flaky` | CONFIRMED | `--retries=1 --grep "@RETRY-FLAKY-001"` | `tests[].status: "flaky"`, `results` = `[{retry:0,failed},{retry:1,passed}]`, `stats.flaky: 1` | Modelo `FAIL→PASS = flaky` válido |
| 4 | `retries=1`: fallo → fallo es fallo estable | CONFIRMED | `--retries=1 --grep "@RETRY-REPEAT-001"` | `status: "unexpected"`, `results` = `[failed, failed]` (retry 0 y 1), `stats.unexpected: 1`, exit 1 | `fail` + `stability: repeated_in_run` válido |
| 5 | El exit code refleja `flaky` | **REJECTED** | `--retries=1 --grep "@RETRY-FLAKY-001"` | Exit **0** con un test flaky. `--fail-on-flaky-tests` (existe desde 1.45 según las release notes) da exit 1 pero el JSON sigue diciendo `flaky` | El normalizador decide por el JSON, no por el exit code. Exit 0 no basta para un pass |
| 6 | `trace: on-first-retry` genera trace del fallo | **REJECTED** (parcial) | `--retries=1 --trace=on-first-retry` | Traza solo del **retry 1**. En un flaky, la única traza es la del intento que **pasó**; el fallo inicial solo deja screenshot y `error-context.md`. Con `--retries=0` **no hay ninguna traza** | El modo por defecto del diseño pierde la evidencia del primer fallo. Ver correcciones |
| 7 | Modos de trace alternativos | CONFIRMED | `--trace=<modo>` | `retain-on-first-failure`: traza del attempt 0 fallido (también con `retries=0`). `retain-on-failure`: todos los attempts fallidos. `on-all-retries`: todos los retries. `retain-on-failure-and-retries`: ambos. Todos aceptados por CLI | `retain-on-first-failure` es el mejor por defecto |
| 8 | CLI de traces sin abrir la UI | CONFIRMED | `npx playwright trace open/actions/errors/requests/console/snapshot/screenshot/attachments/close` | Ver sección "Trace capabilities". Salida de texto, sin opción JSON | No hace falta parser propio de `trace.zip`, pero tampoco se puede parsear la salida de forma robusta |
| 9 | Expected/Received extraíbles | PARTIAL | inspección de `errors[].message` | Ver sección "Expected / Received". Semi-estructurado | Almacenar el mensaje como texto; extraer Expected/Received solo de forma oportunista |
| 10 | Tags en el JSON | CONFIRMED (con matiz) | `tag: ['@behavioral','@AUTH-LOGIN-001']` | En `specs[].tags` salen **sin `@`**: `["behavioral","AUTH-LOGIN-001"]`. En el título (grep, reporter `line`) sí llevan `@` | Mapear `tags` con `@` ausente; el grep sí lleva `@` |
| 11 | `--grep` por ID sin colisión de prefijos | **REJECTED** el grep ingenuo; CONFIRMED el lookahead | `--grep "@AUTH-LOGIN-001"` | Ingenuo: **ejecuta 2 tests** (`-001` y `-0010`). `"@AUTH-LOGIN-001(?![\w-])"` → 1 test; `"@AUTH-LOGIN-0010(?![\w-])"` → 1 test. También válidos `"@AUTH-LOGIN-001$"` y `"@AUTH-LOGIN-001( \|$)"` | El rerun por ID usa el lookahead. Pasar el patrón como argv, sin shell |
| 12 | Annotations `covers` | CONFIRMED | `annotation: {type:'covers', description:'issue:151'}` | `tests[].annotations[] = {type, description, location:{file(absoluto), line, column}}`; también en `results[].annotations` y en `--list` | `covers` es legible desde el JSON y desde `--list` |
| 13 | Cero tests nunca será PASS (detectable) | CONFIRMED | `--grep "@DOES-NOT-EXIST"` | Exit **1**, JSON escrito con `suites: []`, `errors: [{message:"Error: No tests found"}]`, stats a 0. Con `--pass-with-no-tests`: exit 0, sin errores, `suites: []`, stats a 0. `--only-changed` sin cambios: exit 0 y cero tests | Regla: `executed == 0` → no pass, **con independencia del exit code**. No usar `--pass-with-no-tests` |
| 14 | `forbidOnly` | CONFIRMED | `-c playwright.only.config.ts --forbid-only` | Exit 1, JSON con `suites: []`, stats a 0 y `errors: [{message:"Error: item focused with '.only' is not allowed due to the '--forbid-only' CLI flag: …"}]`, `config.forbidOnly: true` | Pasar siempre `--forbid-only` |
| 15 | `test.only` sin `forbidOnly` | CONFIRMED (hallazgo nuevo) | `-c playwright.only.config.ts` | Exit **0**. Los demás tests del fichero **desaparecen del JSON** (ni siquiera salen como `skipped`) | Un run parcial puede parecer pass. Mitigación: `--forbid-only` siempre y comparar la selección con `--list` |
| 16 | `skipped` | CONFIRMED | `--grep "@MISC-SKIP-001"` | `status: "skipped"`, `expectedStatus: "skipped"`, `results: [{status:"skipped"}]`, exit 0, `stats.expected: 0`, `stats.skipped: 1` | Un test solo skip no es pass: `executed == 0` |
| 17 | Error de `beforeEach` | CONFIRMED | `--grep "@MISC-SETUP-001"` | `status: "unexpected"`, resultado `failed`, exit 1. El JSON **no marca que fue un hook**; solo `errors[].location` apunta a la línea del hook. La traza sí lo marca: `beforeEach hook ✗` | `setup` se distingue de forma fiable solo con traza; desde el JSON es heurístico |
| 18 | Fallo de `webServer` | CONFIRMED | config con `webServer.command` que sale con código 3 | Exit 1, JSON con `errors: [{message:"Error: Process from config.webServer was not able to start. Exit code: 3"}]` y stats a 0. La ejecución tardó 134 s a pesar de `timeout: 5000` | Estados a 0 + `errors[]` de nivel superior = `error/blocked`, no `fail` |
| 19 | App inaccesible en tiempo de ejecución | PARTIAL | `baseURL` a un puerto cerrado | `status: "timedOut"`, `errors[]` con varias entradas: "Test timeout of 15000ms exceeded." y "page.goto: net::ERR_ABORTED…". Tardó 25 s. Puede depender del sandbox de este entorno | No es distinguible de forma fiable por el estado; hay que escanear todo `errors[]` en busca de `net::ERR_` y hacer un preflight de salud (`cc-run`) |
| 20 | `screenshot`/`video` por CLI | **REJECTED** | `npx playwright test --help` | No existen flags `--screenshot`/`--video`; **solo** `playwright.config`. Sí existen `--trace`, `--retries`, `--output`, `--reporter`, `--forbid-only`, `--fail-on-flaky-tests`, `--pass-with-no-tests`, `--only-changed`, `--last-failed`, `--repeat-each`, `--workers`, `--timeout`, `--max-failures`, `--list` | La config del proyecto debe fijar screenshot y video (o un config derivado) |
| 21 | `screenshot: only-on-failure` / `video` | CONFIRMED | variantes por entorno en la config | Screenshot `only-on-failure`: `test-failed-N.png` por intento fallido; `on`: `test-finished-N.png`. Video `off` → ninguno; `on-first-retry` → solo retry 1; `retain-on-failure` → attempts fallidos; `on` → todos. Un `.webm` mínimo pesa ~4 KB | Vídeo opcional y barato aquí, pero no representativo de apps reales |
| 22 | `error-context.md` (no estaba en el diseño) | CONFIRMED (nuevo) | cualquier fallo | Se genera en **cada** intento fallido, incluso con `retries=0`, `screenshot=off` y sin traza. ~2 KB: error, Expected/Received, árbol de accesibilidad en YAML y el código fuente del test. Adjunto en el JSON como `error-context` (`text/markdown`) | Es la mejor fuente de nivel 2: compacta y siempre presente |
| 23 | `--only-changed` | CONFIRMED | `--only-changed` / `--only-changed=HEAD~1` | Funciona sobre árbol sucio y con ref. Granularidad de **fichero**. Sigue imports: un cambio en un helper importado ejecutó el spec dependiente (un import sin uso no cuenta). Sin cambios: exit 0 y cero tests | Mejora futura viable (nivel de fichero) con la salvedad del exit 0 |
| 24 | `--last-failed` | CONFIRMED | `--last-failed` | Reejecuta solo los fallidos usando `<output>/.last-run.json` (IDs de spec de Playwright, no los nuestros) | No lo necesitamos; reruns por ID explícito |

## JSON reporter mapping

Forma real (1.63.0): `{config, suites[], errors[], stats}`. `suites[]` por fichero, anidados por `describe`. `suites[].specs[]` → `tests[]` → `results[]` (uno por attempt).

| Canonical field | Playwright source | Notas |
|---|---|---|
| `testId` | `specs[].tags[]` (el que cumple el patrón `AREA-NAME-NNN`) | Sin `@` en el JSON. Sin tag válido → `unidentified`, con fallback `file::título` |
| `title` | `specs[].title` | Los títulos de `describe` salen en `suites[].title` anidados |
| `file` | `specs[].file` | **Relativo a `testDir`** (p. ej. `fail.spec.ts`). El absoluto sale en `annotations[].location.file` y `errors[].location.file` |
| `line` / `column` | `specs[].line`, `specs[].column` | |
| `tags` | `specs[].tags[]` | Incluye `behavioral` |
| `covers` | `tests[].annotations[]` con `type: "covers"` | `description` es el valor (`issue:151`) |
| `status` (del test) | `tests[].status` | `expected`, `unexpected`, `flaky`, `skipped`. `expectedStatus` indica lo esperado |
| `ok` | `specs[].ok` | `true` para pass, flaky y skipped |
| `attempts` | `tests[].results[]` | Un elemento por attempt |
| `attempt index` | `results[].retry` | 0 = primer intento |
| `attempt status` | `results[].status` | `passed`, `failed`, `timedOut`, `skipped` (más `interrupted`, no probado) |
| `duration` | `results[].duration` (ms) y `stats.duration` | |
| `startedAt` | `results[].startTime`, `stats.startTime` | ISO 8601 UTC |
| `error` | `results[].errors[]` (`{message, location}`) y `results[].error` (`{message, stack, location, snippet}`) | `message` con códigos ANSI y code frame incluido. Puede haber **varias** entradas |
| `error location` | `results[].errorLocation` | Ruta absoluta |
| `attachments` | `results[].attachments[]` (`{name, contentType, path}`) | Nombres vistos: `screenshot`, `video`, `trace`, `error-context`. `path` **absoluto** |
| `run-level errors` | `errors[]` de nivel superior (`{message}`) | `No tests found`, `--forbid-only`, fallo de `webServer` |
| `counts` | `stats.expected/unexpected/flaky/skipped` | Cuentan **tests**, no attempts. No hay campo de "ejecutados" |
| `forbidOnly`, `retries`, … | `config.forbidOnly`, `config.projects[]`, `config.metadata.actualWorkers` | `config.grep` salió `{}` aun con `--grep` |

Tamaños observados: ~88 KB para 12 tests con retries (≈ 7,4 KB por test). **El JSON crudo nunca debe entrar en el contexto del agente.**

Contiene rutas absolutas del host (`config.argv`, `rootDir`, `configFile`, anotaciones, adjuntos): el normalizador debe relativizarlas antes de que lleguen a un finding o a un comentario.

## Exit code matrix

| Escenario | Exit | JSON escrito | Detección en JSON |
|---|---|---|---|
| pass | 0 | sí | `stats.expected ≥ 1`, `unexpected == 0` |
| fail (assertion/selector/http) | 1 | sí | `status: unexpected` |
| flaky (`retries=1`) | **0** | sí | `status: flaky`, 2 results |
| flaky con `--fail-on-flaky-tests` | 1 | sí | igual que arriba (el JSON no cambia) |
| fallo repetido (`retries=1`) | 1 | sí | `unexpected`, 2 results fallidos |
| skipped solo | **0** | sí | `stats.skipped == 1`, `expected == 0` |
| cero tests (`--grep` sin match) | 1 | sí | `suites: []`, `errors: "No tests found"` |
| cero tests + `--pass-with-no-tests` | **0** | sí | `suites: []`, sin errores |
| `--only-changed` sin cambios | **0** | sí | cero tests |
| `test.only` sin `--forbid-only` | **0** | sí | **los demás tests ausentes** |
| `test.only` con `--forbid-only` | 1 | sí | `errors: "….only is not allowed…"`, `forbidOnly: true`, `suites: []` |
| error de `beforeEach` | 1 | sí | `unexpected`, error en la línea del hook |
| `webServer` falla | 1 | sí | `errors[]` de nivel superior, stats a 0 |
| app caída durante el test | 1 | sí | `timedOut`, `errors[]` con `net::ERR_*` |

Conclusión: **el exit code no es suficiente**. Un pass exige exit 0 **y** JSON válido **y** `executed ≥ 1` **y** `unexpected == 0` **y** `flaky == 0` **y** todos los IDs seleccionados presentes y ejecutados.

## Trace capabilities

Probado con `trace.zip` de un fallo real (Playwright 1.63.0). `npx playwright trace --help` lista: `open`, `close`, `actions`, `action`, `requests`, `request`, `console`, `errors`, `snapshot`, `screenshot`, `attachments`, `attachment`, `install-skill`.

| Capacidad | Comando | Resultado |
|---|---|---|
| Metadatos | `trace open <zip>` | Playwright, plataforma, título, duración, nº de acciones, errores, requests, consola, adjuntos. El campo `Browser` salió `unknown` |
| Acciones/pasos | `trace actions` (`--grep <re>`, `--errors-only`) | Árbol numerado con tiempos; `Expect "toBe" … ✗` marca la acción fallida. Con hooks: `beforeEach hook ✗` |
| Errores con stack | `trace errors` | Mensaje con ANSI y ubicación |
| Requests | `trace requests` (`--failed`, `--grep`, `--method`) | Tabla: inicio, método, status, URL, duración, tamaño. `--failed` mostró el 404 |
| Detalle | `trace request <id>`, `trace action <id>` | `action` probado; `request` no se ejecutó |
| Console | `trace console` (`--errors-only`, `--warnings`, `--browser`, `--stdio`, `--grep`) | Funciona; salió el error de consola del navegador |
| Snapshot de accesibilidad | `trace snapshot <action-id> [--phase before\|action\|after]` | YAML del árbol de accesibilidad (no HTML). Solo para acciones que tienen snapshot: la acción `Expect` devolvió `No snapshot found` y la `Navigate` sí |
| Screenshot de una acción | `trace screenshot <action-id> -o <png>` | Guarda un PNG |
| Adjuntos | `trace attachments`, `trace attachment <n>` | `attachments` probado (lista screenshot, video, error-context); `attachment <n>` no se ejecutó |

Limitaciones comprobadas:

- **La CLI es con estado:** `trace open` extrae a un directorio `.playwright-cli/` en el **cwd** y hay que cerrar con `trace close`. Ejecutarla dentro de un workspace `read_only` crearía un artefacto ahí: el nivel 2 debe usar como cwd el directorio de evidencia.
- **Sin salida JSON:** la salida es texto (con ANSI en `errors`). Sirve para entregar al agente de forma acotada (`| head`), no para parsearla de forma robusta.
- **No probado:** ejecución concurrente de varios `trace open`, ni versiones anteriores a 1.63.0.

## Expected / Received

| Matcher | Mensaje (sin ANSI) | Clasificación |
|---|---|---|
| `toBe` | `Expected: "5"` / `Received: "3"` | semi-structured |
| `toEqual` | formato diff (`- Expected - 1`, `+ Received + 1`, líneas `-`/`+`), sin líneas `Expected:`/`Received:` simples | not reliable (texto) |
| `toHaveText` | `Locator: …`, `Expected: "Dashboard"`, `Received: "Login"`, `Timeout:`, `Call log` | semi-structured |
| `toBeVisible` | `Expected: visible`, `Error: element(s) not found`; **sin `Received`** | semi-structured, incompleto |
| selector (`locator.click`) | `TimeoutError: locator.click: Timeout 1500ms exceeded.` + `Call log`, **sin Expected/Received** | not reliable |
| status HTTP vía `toBe` | `Expected: 200` / `Received: 404` | semi-structured |

No hay campos `expected`/`received` en el JSON: el mensaje es una cadena con ANSI y con un code frame incluido. **Clasificación global: semi-structured.** Recomendación para el Issue 2: guardar el mensaje (sin ANSI, truncado a N líneas) como texto opaco en `observed`/`error`; extraer `Expected:`/`Received:` solo con una regex oportunista por línea cuando ambas existan, y dejar `null` en caso contrario. Nunca fallar por no poder extraerlos.

## Artefactos y salida

Con `--output=<dir>`: `<dir>/<fichero>-<título-saneado>[-retryN]/` con `test-failed-N.png`, `video.webm`, `trace.zip`, `error-context.md`; y `<dir>/.last-run.json`. **El nombre de carpeta no contiene el testId**: el mapeo se hace por `attachments[].path` del JSON (rutas absolutas). Una traza se asocia a su attempt por el sufijo `-retryN`.

## Versión mínima recomendada

Todo se ejecutó en **1.63.0** (la última estable). Recomendación: **exigir ≥ 1.63.0** para behavioral verification y registrar la versión exacta en el receipt. No hay datos de versiones anteriores y no se justifica inventar un mínimo más bajo: la CLI de traces, `error-context.md` y `retain-on-first-failure` no se han verificado en otras versiones (solo `--fail-on-flaky-tests` aparece documentado desde 1.45). `behavioral.py doctor` debe **sondear capacidades** (que `trace --help` liste `actions`, que `test --help` liste `--only-changed`) además de comparar la versión.

## Required design corrections

Cambios necesarios en `docs/behavioral-verification-design.md` (no aplicados todavía):

1. **Modo de trace por defecto (sección I):** sustituir `on-first-retry` por `--trace=retain-on-first-failure`. `on-first-retry` no deja traza con `retries=0` y, en un flaky, solo la del intento que pasó. Quitar la frase "solo graba el reintento (la confirmación)". Coste: graba todas las ejecuciones y conserva solo la del primer fallo.
2. **`test.only` (sección I, reglas de PASS):** añadir que sin `--forbid-only` los demás tests desaparecen del JSON con exit 0. `--forbid-only` pasa a ser obligatorio en el comando de referencia y la selección se contrasta con `--list`.
3. **Regla de PASS (sección I):** el exit code no basta (flaky, skip y cero tests pueden dar exit 0). Reescribir como `exit 0 ∧ JSON válido ∧ executed ≥ 1 ∧ unexpected = 0 ∧ flaky = 0 ∧ selección completa`. Añadir que `executed` no existe en el JSON y se calcula.
4. **Rerun por ID (secciones K y C):** el grep ya tiene la forma `@ID(?![\w-])`; añadir que el ingenuo colisiona (comprobado) y que el patrón se pasa por argv.
5. **Tags (sección C):** aclarar que el JSON los entrega **sin `@`**.
6. **Niveles de evidencia (secciones G y H):** añadir `error-context.md` (siempre generado en fallos, ~2 KB, con árbol de accesibilidad) como fuente primaria del nivel 2, antes de la traza. Ajustar la tabla de artefactos (RECOMMENDED) y la lista de obligatorios.
7. **CLI de traces (sección H):** sustituir "a validar" por los comandos reales (`actions --errors-only`, `requests --failed`, `console --errors-only`, `errors`, `snapshot <id>`, `screenshot <id>`, `attachments`) y documentar que **sí existe `console`**, que no hay salida JSON y que la CLI es con estado (`.playwright-cli/` en el cwd; `trace close`). El nivel 2 debe ejecutarse con cwd = directorio de evidencia.
8. **Configuración (sección I):** `screenshot` y `video` solo se fijan en la config (no hay flags); sí se pueden forzar por CLI `--reporter`, `--trace`, `--retries`, `--output`, `--forbid-only`. Ajustar la afirmación de la tabla y decidir en el Issue 2 si el toolkit usa la config del proyecto o una derivada.
9. **Reporters (sección I):** documentar que `--reporter` por CLI **sustituye** los reporters del proyecto.
10. **Taxonomía (sección L):** `setup` solo es fiable con traza (el JSON no marca hooks); `environment` por timeouts requiere escanear todo `errors[]` (`net::ERR_*`) y un preflight de salud; estado a 0 más `errors[]` de nivel superior = `error/blocked`.
11. **Cero tests y `--only-changed` (secciones K y P):** `--only-changed` queda como mejora futura viable (granularidad de fichero, sigue imports) con la salvedad de que "sin cambios" da exit 0 y cero tests; `--pass-with-no-tests` no debe usarse.
12. **Seguridad (sección O):** el JSON contiene rutas absolutas del host; el normalizador las relativiza antes de publicar nada.
13. **Evidencia completa (#88):** en este entorno el hook RTK compactó la salida de `npx playwright` ("RTK:PASSTHROUGH"). Reforzar que el normalizador lee **solo el JSON a fichero** y el exit code, nunca stdout.

## Recommendation for Issue 2

`behavioral.py` puede asumir con seguridad (Playwright ≥ 1.63.0):

- Invocación: `npx playwright test --reporter=line,json --retries=1 --trace=retain-on-first-failure --output=<dir> --forbid-only --grep <regex>` con `PLAYWRIGHT_JSON_OUTPUT_NAME=<run>/raw/playwright.json`, pasada como argv (sin shell), cwd del proyecto.
- Registro: `npx playwright test --list --reporter=json` entrega `specs[].{title,file,line,column,tags}` y `tests[].annotations` (incluido `covers`), con `status: skipped` y `results: []` por ser listado.
- Estructura estable: `config`, `suites[]→specs[]→tests[]→results[]`, `errors[]`, `stats`. Campos usables: `tests[].status`, `results[].{retry,status,duration,startTime,errors,attachments}`, `stats`.
- Reglas de decisión, todas por JSON y no por exit code: ver "Exit code matrix". `executed` se calcula como tests con al menos un resultado cuyo estado no sea `skipped`.
- Texto opaco, no estructura: `errors[].message` (sin ANSI, truncado). Extracción de `Expected`/`Received` opcional.
- Evidencia nivel 2: `error-context.md` primero; después los comandos de la CLI de traces con cwd en el directorio de evidencia y siempre `trace close`.
- No asumir: `expected`/`received` estructurados, un campo "executed", marcado de hooks en el JSON, coincidencia entre nombre de carpeta de artefactos y testId, salida JSON de la CLI de traces, comportamiento en versiones anteriores a 1.63.0, ni concurrencia de `trace open`.
