# Behavioral verification: design (phase 2)

Estado: propuesta de diseño, sin implementar. Fecha: 2026-10-05.

Arquitectura decidida: el toolkit es dueño de la behavioral verification; Playwright es el sustrato determinista; Midscene es una capa AI opcional (fase 2 del plan); el toolkit normaliza el resultado. SkyTest y Quorvex no forman parte del diseño base.

**Revisión 1 (2026-10-05).** Review arquitectónica previa: APPROVE WITH CHANGES. Cambios aplicados en este documento: retry ≠ reproducción independiente (E), fingerprint por posición lógica (E), `behavioral_required` con enforcement en runtime dentro del MVP (D, P), ejecución única por `behavioral.py` (D), semántica estricta de `not_configured/blocked/error` (D), `covers` con referencias duraderas (C), regresión afectada reducida (K), claim de `allowed_origins` rebajado (O), raw de Playwright con retención configurable (G) y MVP condensado a 6 issues (P).

**Revisión 2 (2026-10-05).** Contradicciones internas de la revisión 1 resueltas y plan contrastado con las issues abiertas (#146, #145, #141, #151, #147) y cerradas (#85, #86, #88, #83, #77). Cambios: telemetría unificada en **una sola columna `behavioral_status`** (esquema 14, MVP); `behavioral_verified` se deriva y `behavioral_required` se calcula; la verificación behavioral cuenta como evidencia de boundary para el área `ui` (#86); `artifacts` reutiliza la forma de #146; ruta de evidencia por ciclo (#141); `base_url` viene de `cc-run` (#151); coordinación con #145; restricción de roles `read_only` y decisión pendiente (sección D); correcciones de `retries`, de la decisión de rerun y del criterio de aceptación de `fp`.

**Revisión 3 (2026-10-05).** Decididos: opción (a) para roles `read_only` (verify/run ejecuta, rereview consume el resultado del mismo HEAD, el orquestador despacha lo que falte) y `level: behavioral` propio que satisface el boundary `ui`. Issue 5 desbloqueado.

**Revisión 4 — resultados del spike (2026-10-05).** El spike de Playwright (`docs/spikes/playwright-behavioral-capabilities.md`, Playwright 1.63.0) no cambia la arquitectura pero corrige supuestos operativos. Incorporado: trace por defecto `retain-on-first-failure` (no `on-first-retry`); `--forbid-only` obligatorio y selección contrastada con `--list`; PASS definido por JSON + selección + conteos, no por exit code; grep `@ID(?![\w-])` por argv sin shell; `--reporter` por CLI sustituye a la config y `screenshot`/`video` solo en `playwright.config`; `error-context.md` como fuente prioritaria de nivel 2; `Expected/Received` como best-effort con el mensaje completo sin ANSI siempre conservado; `setup` y `environment` explícitamente heurísticas; trace CLI con estado (cwd = directorio de evidencia, `trace close`); el normalizador lee solo `raw/playwright.json`, nunca stdout; `Playwright >= 1.63.0` como mínimo comprobado con sondeo de capacidades en `doctor`; `--only-changed` como mejora futura validada; cold start anotado como consideración operativa. Issue 1 (spike) completado; el Issue 2 puede empezar.

**Qué está comprobado y qué no.** El estado del toolkit (sección A) se leyó en el repo. Lo que se dice de Playwright viene de su documentación actual (Context7) y **no se ha ejecutado**; se marca como "a validar en el spike".

## A. Estado actual del toolkit

| Tema | Hoy |
|---|---|
| `cc-verify` | Skill en Markdown, sin código propio. Cada comando ejecutado se registra con `level: static\|test\|boundary`. Conclusión: `verified`, `verified_with_reservations`, `not_verified` o `failed`, dentro de un bloque `VERIFICATION_RESULT`. |
| Boundary | `boundary_required = regla determinista OR juicio del verificador` (regla por rutas en `scripts/stage_signals.py`). Si es obligatorio y no se ejecutó, el máximo es `verified_with_reservations` con razón `environment`, `cost` u `out_of_scope`. |
| UI | El paso 8 de `cc-verify` pide recorrer el flujo en navegador, a mano. No deja artefactos ni identidad de test. |
| Stop conditions | Entorno, servicio, puerto, migraciones o permisos: se para y se pregunta; no se degrada a "no verificado". |
| Runtime | `run_cycle._tests()` valida `EVIDENCE_LEVELS`, calcula `tests_basis` (`claimed` → `agent_reported` → `runtime_observed` → `externally_verified`) y `boundary_verified`. El runtime nunca ejecuta la verificación. |
| Findings | Viven en el comentario del PR, no en una BD. Cabecera: `[REV-004] · medium · resolved · valid · blocks:yes — título`. Estados `open/resolved/not_applicable`, disposición congelada, `SCHEMA_VERSION = 1`. Recuperación solo desde `trusted_authors`. |
| Supervivencia de fixes | Un finding "sobrevive a un fix reclamado" cuando se publica `resolved` o `not_applicable` y la rereview lo reabre. De ahí sale la escalera `repeated_findings`. |
| "Receipts" | **No hay receipts de verificación.** "Receipt" en el repo es el recibo de despacho del executor (Orca). Lo más parecido: `VERIFICATION_RESULT`, las filas `verdict` de telemetría (SQLite, esquema 13; solo estados, conteos y flags) y el patrón del fichero de diff (ruta + SHA-256 bajo `CODE_CYCLE_HOME/evidence/`, fuera del workspace). |
| `ready` | `READY_FOR_MANUAL_MERGE` exige: último review `APPROVED`, `reviewed_head_sha` igual al head actual, ningún finding `blocks:yes` abierto, checks requeridos en verde. |
| `cc-doctor` | No existe. Lo más cercano: `cc-provider-bootstrap`, `scripts/installed_stage_check.py`, `scripts/validate-package.py`. |
| Config | `.code-cycle.yml` bajo `code_cycle:`. El espacio `verification.*` ya está ocupado (caché de salud de proveedores). |

## B. Cambios mínimos necesarios

Un nivel de evidencia nuevo, no un subsistema paralelo:

1. `scripts/behavioral.py` (Python, como el resto del runtime y sus `unittest`): ejecuta Playwright y normaliza el resultado.
2. `cc-verify`: nuevo `level: behavioral`.
3. `run_cycle.EVIDENCE_LEVELS` acepta `behavioral`.
4. Reglas de texto en las skills de review, resolve y rereview.
5. Claves de config y documentación.

Un único cambio de telemetría (esquema 14: columna `behavioral_status`, ver sección D). Sin cambios en la gramática de cabeceras de `review_contract.py` ni IDs nuevos.

## C. Modelo canónico (mínimo)

| Entidad | Identidad | Dónde vive | Notas |
|---|---|---|---|
| **BehavioralTest** | `AREA-NAME-NNN` en un tag de Playwright | Se deriva de los `.spec.ts`; no se persiste un objeto aparte | `id`, `file`, `title`, `tags`, `covers[]`. Registro vía `playwright test --list --reporter=json`. |
| **BehavioralRun** | `bv-<fecha>-<hash>` | `run.json` | `commit`, `dirty`, `selection`, `command`, `configHash`, `tool`, `toolVersion`, `browser`, `startedAt`, `finishedAt`, `status`, `counts`, `schema:1`. |
| **BehavioralResult** | `(runId, testId)` | Dentro de `run.json` | `status` (`pass/fail/flaky/skipped/error`), `attempts[]`, `executionMode`, `durationMs`, `failure?`, `evidence[]`. |

No se crean como entidades propias:

- **BehavioralFailure**: bloque embebido en el resultado (`fingerprint`, `category`, `step`, `expected`, `observed`, `location`).
- **Evidence**: referencia, no contenido (`{kind, path, sha256, bytes, attempt}`).
- **FailureBundle**: vista derivada de `run.json` más los artefactos de un test fallido.

Ciclo de vida del run: `created → running → finished(status)`. Inmutable una vez `finished`; reintentar crea otro run. Versionado con `schema:1` dentro del JSON.

### Identidad de los tests

- Tag de Playwright con ID estable, independiente del fichero y del título:

  ```ts
  test('login rejects invalid password', {
    tag: ['@behavioral', '@AUTH-LOGIN-001'],
    annotation: { type: 'covers', description: 'issue:151' },
  }, async ({ page }) => { /* ... */ });
  ```

- En el reporter JSON los tags salen **sin `@`** (`["behavioral","AUTH-LOGIN-001"]`; comprobado en ejecución). En el título y en el grep sí llevan `@`.
- Rerun individual: `--grep "@AUTH-LOGIN-001(?![\w-])"`. El grep ingenuo `@AUTH-LOGIN-001` **colisiona** (comprobado: ejecuta también `AUTH-LOGIN-0010`); el lookahead lo evita. El patrón se pasa **como argv**, nunca por una cadena de shell.
- Registro de tests: `npx playwright test --list --reporter=json` entrega `title`, `file` (relativo a `testDir`), `line`, `column`, `tags` y las `annotations` (incluido `covers`).
- `behavioral.py list` detecta IDs duplicados o ausentes; un test sin ID se reporta como `unidentified` (fallback `archivo::título`).
- Renames: el ID persiste aunque cambien el fichero o el título.
- Requisito/issue: anotación `covers` con referencias **duraderas**: `issue:151`, `requirement:AUTH-LOGIN`, `bug:GH-834`. `review-finding:REV-014` se admite solo como vínculo temporal mientras se resuelve el PR y no es el mecanismo de trazabilidad histórica (`REV-nnn` solo tiene sentido dentro de su review).
- **No se añade YAML declarativo** (`.behavioral/*.yaml`): sería una segunda fuente de verdad. Los `.spec.ts` se revisan directamente en el diff del PR y la trazabilidad va en anotaciones.

## D. Flujo completo

```text
cc-implement / cc-resolve-comments / cc-rereview
        │ (cambios en rutas "behavioral required" + tests configurados)
        ▼
cc-verify ── static ── test ── boundary ── behavioral ◄── cc-run (arranca la app)
                                              │
                                    scripts/behavioral.py
                                              │ npx playwright test (--grep por ID/tag)
                                              ▼
                         playwright.json + artifacts (trace/screenshot)
                                              │ normalizador
                                              ▼
              <evidence>/behavioral/<run>/{run.json, summary.md, failures/*}
                                              │
              VERIFICATION_RESULT: evidence[{level:"behavioral", run_id, sha256, …}]
                                              ▼
 review/rereview ─► REV-nnn (cuerpo: "behavioral: TEST-ID fp:… run:…") ─► resolve ─► rerun por ID ─► @smoke ─► suite (si disparador crítico)
                                              ▼
              ready (head SHA + checks; el job CI "behavioral" es externally_verified)
```

### Encaje con `cc-verify`

Es un **nivel de evidencia** (`level: behavioral`) dentro del flujo actual, no una fase aparte ni un verifier especializado.

**`behavioral_required` se aplica en runtime, en el MVP.** `behavioral_required = regla determinista OR juicio del verificador`. La regla es `behavioral_required_by_paths(changed_files, config)` sobre `required_when.paths`, evaluada por el runtime (junto a `boundary_rule_fired` en `run_cycle`, siguiendo el patrón de `scripts/security_gate.py`). El agente puede ampliar la obligación, nunca reducirla. No hace falta `touches_ui`: basta la regla por rutas de config.

Tres conceptos separados:

```text
behavioral_required   ← se calcula (regla OR juicio); no se almacena
behavioral_status     ← se registra (única columna nueva, esquema 14)
behavioral_verified   ← se deriva: behavioral_status == "pass"
```

`behavioral_status` toma los valores `NULL` (= `not_applicable`), `not_configured`, `blocked`, `error`, `skipped`, `fail`, `flaky`, `pass`. Un booleano perdería el motivo; con el estado se sabrá meses después **por qué** no hubo verificación.

Lógica en `run_cycle`:

```text
required = behavioral_required_by_paths(...) or agent_requests
status   = behavioral_status

if required and status != "pass":
    flaky | not_configured | skipped → conclusión máx. verified_with_reservations
    error                            → not_verified
    fail                             → failed
    blocked                          → stop condition (se para y se pregunta)
```

**Relación con boundary (#86).** La verificación behavioral es la evidencia de boundary para el área `ui` (se añade la fila "UI/browser flow" a la tabla de boundary): un `behavioral_status == pass` satisface un requisito de boundary de ese área. `boundary_verified` no cambia de significado ni de columna. **Decisión: `level: behavioral` propio**, no `level: boundary` con `kind`. Son dimensiones distintas: `behavioral` es qué evidencia se ejecutó (con `run_id`, conteos, selección, hashes y estado, mucho más rica que la evidencia de boundary) y `boundary` es qué requisito satisface. Un mismo resultado tiene `level: behavioral` y satisface el boundary `ui`; mañana otra evidencia de UI (regresión visual, accesibilidad, dispositivo) podría satisfacer lo mismo sin forzarla dentro de `boundary`.

**Una sola ruta de ejecución.** Toda ejecución behavioral pasa físicamente por `scripts/behavioral.py` (`run`, `list`, `evidence`, `doctor`). Las skills (`cc-verify`, `cc-rereview`, `cc-resolve-comments`) solo deciden **qué** ejecutar y **por qué**; ninguna reimplementa selección, ejecución, rutas de evidencia, normalización ni interpretación de exit codes.

### Quién ejecuta: decisión (a)

`docs/role-workspace-policy.md`: `review`, `rereview`, `issue_review` y `security` son `read_only`; `verify` y `run` son `disposable`. **Decisión: la reproducción no la hace el reviewer ni el runtime; la hace un stage `verify/run` en workspace desechable y el reviewer consume su resultado.** El runtime sigue sin ejecutar verificación (decisión de #86 intacta).

```text
review → REV behavioral → resolve
   → targeted verify (disposable): behavioral.py run --id AUTH-LOGIN-001
        ├─ FAIL → vuelve a resolve
        └─ PASS → rereview (read_only): consume el resultado del mismo HEAD
                  → valida fix + evidencia → APPROVED / CHANGES_REQUESTED
```

| Quién | Hace | No hace |
|---|---|---|
| stage `verify/run` | Ejecuta `behavioral.py` y produce el `BehavioralRun` | Juzga si el cambio es correcto |
| `cc-rereview` | Consume un `BehavioralRun` válido y revisa el diff acumulado | Ejecutar tests behavioral |
| runtime / orquestador | Calcula `behavioral_required`, comprueba que exista la evidencia necesaria, **despacha** el stage que falte y registra `behavioral_status` | Ejecutar Playwright |

`verify` demuestra el comportamiento; `rereview` demuestra que el cambio es correcto. No se mezclan.

**Evidencia válida para rereview (regla de HEAD).** Solo se acepta un `BehavioralRun` si `run.commit == HEAD actual` y `dirty == false`. Si el código cambia después (p. ej. el run pasó en `abc123` y el HEAD es `def456`), el resultado queda **stale** y no puede cerrar el finding. Es la misma filosofía que `reviewed_head_sha == HEAD` en `ready`.

**Regla de orquestación.** Cuando un finding behavioral abierto se marca como resuelto, antes de `cc-rereview` debe existir un `BehavioralRun` para el `testId` afectado sobre el HEAD actual. Si no existe, el ciclo despacha un stage `verify/run` en workspace desechable; el reviewer no se detiene a preguntar. `cc-rereview` nunca ejecuta behavioral tests directamente.

```text
resolve → ¿hay REV behavioral reclamado como fixed?
   ├─ no → rereview
   └─ sí → ¿BehavioralRun del HEAD actual para ese testId?
             ├─ sí → rereview
             └─ no → dispatch targeted verify → rereview
```

Efecto en la reproducción: el run inicial con fallo y retry fallido da `stability: repeated_in_run`; el `targeted verify` tras el fix es otra invocación de `behavioral.py`, es decir, un **confirmation run** real. Certificar antes del fix que el defecto es reproducible con un segundo run independiente (`independently_reproduced`) no se exige a todos los findings en el MVP: `repeated_in_run` basta para trabajar.

Nota de elegibilidad: Claude no es elegible para roles `disposable`; el perfil auxiliar por defecto usa Codex (sandbox que confina escrituras al cwd desechable). El issue 5 debe respetar esa restricción de ejecutores.

### Semántica de estados

| Estado | Definición estricta | Ejemplo |
|---|---|---|
| `not_applicable` | El cambio no requiere verificación behavioral | Solo documentación |
| `not_configured` | La capacidad nunca se configuró | El proyecto no tiene Playwright, config ni tests behavioral |
| `blocked` | Configurada y requerida, pero falta un prerrequisito recuperable | La app no arranca (el fallo de `webServer` sale como `errors[]` de nivel superior con conteos a 0), puerto ocupado, falta una credencial de runtime, Playwright o navegador sin instalar (instalarlo requiere preguntar) |
| `error` | El propio verificador falló | JSON del reporter inválido o ausente, excepción interna, artefacto corrupto, error de nivel superior no atribuible a un test (conteos a 0 + `errors[]`) |
| `skipped` | Decisión explícita de no ejecutar por coste | `mode: off` o presupuesto agotado |
| `fail` | Se ejecutó y una aserción falló de forma estable | Dashboard no visible |
| `flaky` | Falló y luego pasó dentro de la misma ejecución | Pasó en el reintento |
| `pass` | Ejecución terminada y evidencia persistida | Ver reglas de PASS |

La distinción importa: producto roto ≠ entorno no disponible ≠ verificador roto, y el agente reacciona distinto a cada una.

| Estado | Efecto en `cc-verify` |
|---|---|
| `not_applicable` | Sin entrada de evidencia, como `boundary: not_required`. |
| `not_configured` | No obligatorio: silencio. Obligatorio: `verified_with_reservations` (razón `environment`; un token `not_configured` ampliaría un conjunto cerrado). |
| `skipped` (coste) | `verified_with_reservations`, razón `cost`. |
| `blocked` | Stop condition existente: se para y se pregunta. |
| `error` | `not_verified`. |
| `fail` | `failed` y `tests.passed: false`. |
| `flaky` | Como máximo `verified_with_reservations`. |
| `pass` | Cuenta como `verified`. |

`cc-verify` es una skill y no tiene exit code propio; eso no cambia.

Códigos de salida de `behavioral.py`: `0` pass, `10` fail, `11` flaky, `12` skipped (un test seleccionado quedó sin ejecutar por `skip`/`fixme`), `20` error, `30` blocked, `40` no configurado. Precedencia del estado de un run: `blocked`, `fail`, `error`, `flaky`, `skipped`, `pass` (un fallo real es evidencia aunque la selección esté incompleta; una selección incompleta nunca es pass).

## E. Findings

**Decisión: `REV-*` con el mismo objeto Finding. No crear `VER-*`.**

Motivos: el parser, `next_finding_id`, `blocks`, la recuperación por `trusted_authors`, la escalera `repeated_findings`, el límite de iteraciones y `cc-stats` están atados al prefijo `REV-`. Dos espacios de IDs fragmentarían la regla "sobrevive a un fix". La cabecera no tiene hueco para un tipo; añadirlo obligaría a subir `schema`.

Contrato: una línea en el **cuerpo**, no en la cabecera.

```text
#### [REV-014] · high · open · - · blocks:yes — Login rechaza credenciales válidas tras el cambio
behavioral: AUTH-LOGIN-001 · fp:9c1e4a7b02d1 · run:bv-20261005-1432-ab12 · category:assertion
observed: status 401 (esperado 200)
evidence: sha256:… (run.json) · failures/AUTH-LOGIN-001/bundle.json
```

- **Quién los crea:** solo review y rereview, como hoy. `cc-verify` no publica findings. Un fallo durante implement se corrige sin finding.
- **Identidad (posición lógica, no causa):** `fp` = hash de `testId | identidad del paso o aserción fallida | categoría | ubicación de código estable`. Se guarda aparte `observed_signature` (el síntoma: mensaje y valores observados, sin normalizar de forma agresiva). Así se distingue *mismo finding, misma posición lógica, síntoma distinto* (p. ej. 401 → 500) sin abrir REV nuevos arbitrariamente, y sin fusionar fallos distintos por haber borrado números o parámetros. Mismo `fp` en un rerun reutiliza el REV; si cambia `observed_signature`, se actualiza el cuerpo y se anota el cambio de síntoma. `fp` distinto en el mismo test es otro fallo lógico.
- **Reabrir y cerrar:** el test falla otra vez tras `resolved` → regresión del mismo REV (comportamiento existente). Se cierra solo si rereview encuentra un `BehavioralRun` válido para ese `testId`, producido sobre el mismo HEAD (`dirty == false`) con `status pass`, y confirma además el diff reclamado.
- **Cobertura:** el reviewer puede crear `missing_behavioral_coverage` como REV (por defecto `medium`, `blocks:no`; `blocks:yes` solo en rutas críticas) sin ejecutar nada. Propone ID y pasos. Se cierra cuando existe un test con `covers` que referencia el finding (temporalmente) o el issue o requisito (de forma duradera), que se ejecutó y pasó.
- **No son findings** los errores de entorno o infraestructura: son `error` o `blocked` del run.

### Responsabilidades

```text
cc-review    detecta riesgo y cobertura que falta (puede ejecutar para reproducir; no modifica código)
cc-verify    ejecuta la evidencia behavioral
cc-rereview  valida los fixes reclamados consumiendo un BehavioralRun del mismo HEAD (si falta, el ciclo despacha un stage verify/run previo) y revisa el diff acumulado
```

### Estabilidad y reproducción independiente (idea de Argus)

Se separan dos conceptos con fuerza probatoria distinta:

| Concepto | Qué es | Fuerza |
|---|---|---|
| **attempt** | Reintento dentro de una misma invocación de Playwright (`retries: 1`) | Comparte servicio, base de datos, build, seed y estado externo; **no es independiente** |
| **confirmation run** | Otra invocación de `behavioral.py` (p. ej. el rerun de `cc-rereview`) | Reproducción independiente real |

- Fallo y pass en la misma ejecución = `flaky` (ni PASS verificado ni fallo confirmado).
- Fallo y fallo en la misma ejecución = `fail` con `stability: repeated_in_run`. Es un fallo estable, no "independientemente reproducido".
- Un run posterior que vuelve a fallar añade `confirmedByRun: "bv-…"` y solo entonces el fallo cuenta como `independently_reproduced`.
- `retries: 1` se mantiene porque es barato (solo reejecuta tests que fallan) y detecta flakiness, pero el resultado no se presenta con más fuerza probatoria de la que tiene. Tag `@no-retry` para tests no idempotentes. Para `ai-asserted`, un fallo exige una pasada adicional antes de certificarse.

## F. Receipts

Una entrada más en `evidence[]` del `VERIFICATION_RESULT`, más una única columna de telemetría (`behavioral_status`, esquema 14). Sin tabla nueva.

```json
{ "level": "behavioral", "command": "npx playwright test --grep @behavioral", "exit_code": 0, "executed": 12,
  "run_id": "bv-20261005-1432-ab12", "status": "pass", "passed": 12, "failed": 0, "flaky": 0, "skipped": 0,
  "selection": "tag:behavioral", "run_json_sha256": "…", "commit": "<sha>", "dirty": false,
  "tool": "playwright", "tool_version": "1.xx", "config_hash": "…", "execution_mode": "deterministic" }
```

- Reproducibilidad sin inflar: comando, selección, commit, `dirty`, versión de Playwright, hash de la config y hash de `run.json`. El navegador y los timestamps van en `run.json`.
- Base de evidencia: `agent_reported` (el agente lanza el script; el runtime no lo observa). Pasa a `externally_verified` cuando un job de CI sobre el head SHA ejecuta el mismo script (el runtime ya lee los checks del head).
- Telemetría (MVP): solo `behavioral_status` en el verdict (esquema 14); `behavioral_verified` se deriva y `behavioral_required` se calcula. Recuento de tests y demás columnas, diferidos.
- Artefactos: el array `artifacts` sigue la forma de #146 (`{type, scenario, path}`; `type` incluye `screenshot`, `video` y `trace`), siempre opcional, nunca cambia la conclusión y se trata como `agent_reported`.

## G. Failure bundle

```text
<CODE_CYCLE_HOME>/evidence/behavioral/<cycle_id>/<run-id>/
  run.json                        REQUIRED     BehavioralRun + results[] compacto
  raw/playwright.json             REQUIRED     salida cruda del reporter (retención configurable, ver abajo)
  summary.md                      RECOMMENDED  nivel 1 para el agente (≤ ~60 líneas)
  failures/<TEST-ID>/bundle.json  RECOMMENDED  (si hay fallo) por test
  artifacts/<TEST-ID>/attempt-N/
     error-context.md             RECOMMENDED  en cada intento fallido (~2 KB; error, Expected/Received, árbol de accesibilidad, código del test)
     screenshot.png               RECOMMENDED  en fallo (solo con screenshot: only-on-failure en la config)
     trace.zip                    RECOMMENDED  del primer intento fallido (--trace=retain-on-first-failure)
     video.webm                   OPTIONAL     solo si la config lo activa
```

- Va bajo `CODE_CYCLE_HOME/evidence/behavioral/<cycle_id>/`, con ruta predecible por ciclo (#141: una sola regla de permiso, sin sufijos variables) y fuera del workspace: reutiliza el patrón del fichero de diff (ruta, tamaño, SHA-256) y evita crear un directorio persistente en el repo sin preguntar.
- **Playwright no nombra sus carpetas con el testId** (`<fichero>-<título-saneado>[-retryN]/`). El normalizador reorganiza los artefactos por `<TEST-ID>/attempt-N` mapeando por `attachments[].path` del JSON (rutas absolutas) y el sufijo `-retryN`.
- `raw/playwright.json` es obligatorio **en la ejecución**, no necesariamente para siempre. `run.json` lo referencia con un bloque `source: { kind: "playwright-json", sha256, retained }`, para poder tener más adelante retención corta del raw y larga de `run.json`.
- **El normalizador lee únicamente `raw/playwright.json` (y el exit code del proceso); nunca stdout.** Un host puede compactar la salida de comandos (en este entorno el hook RTK compactó `npx playwright`); es la misma regla de evidencia completa (#88).
- El JSON crudo pesa ~7 KB por test con fallos y **nunca entra en el contexto del agente**. Contiene rutas absolutas del host (`config.argv`, `rootDir`, anotaciones, adjuntos): `run.json` y `summary.md` las guardan relativizadas.
- Obligatorio para considerar verificable la ejecución: `run.json` y `raw/playwright.json` con hashes. Screenshot, trace, error-context y vídeo nunca son obligatorios para un PASS.
- Console, network y DOM **no se generan por defecto**: se extraen bajo demanda del `trace.zip` con la CLI de traces (sección H).
- El bundle no trae hipótesis ni "fix target": el normalizador no usa LLM; eso lo deriva el agente.

## H. Progressive disclosure

| Nivel | Qué ve el agente | Tamaño |
|---|---|---|
| **1** (automático) | `summary.md` normalizado: por test fallido, `id`, título, `archivo:línea`, intentos y estados, paso o aserción fallida, mensaje de error sin ANSI (truncado), `expected`/`observed` **si se pudieron extraer** (best-effort), categoría probable (heurística), flaky o no, punteros a artefactos con tamaño y el siguiente comando. Máximo 5 fallos y "N más". | ~1,5 KB por fallo |
| **2** (a petición) | **Primero `error-context.md`** (error, Expected/Received, árbol de accesibilidad y código del test; ~2 KB, siempre presente en un fallo) más los adjuntos y errores relevantes del resultado. **Solo cuando haga falta**, la CLI de traces vía `behavioral.py evidence <run> <TEST> --kind steps\|errors\|network\|console\|snapshot`. Salida acotada con marcador de truncado y totales. | acotado |
| **3** (con justificación) | `trace.zip` completo, `raw/playwright.json` completo, vídeo. Nunca se cargan solos. | grande |

La traza no es el primer mecanismo de diagnóstico: para muchos fallos `error-context.md` basta.

**CLI de traces (comprobada en 1.63.0).** Comandos: `trace open|close`, `actions` (`--grep`, `--errors-only`), `action <id>`, `errors`, `requests` (`--failed`, `--grep`, `--method`), `request <id>`, `console` (`--errors-only`, `--warnings`, `--browser`, `--stdio`, `--grep`), `snapshot <id>` (`--phase before|action|after`; árbol de accesibilidad en YAML, solo en acciones con snapshot), `screenshot <id> -o <png>`, `attachments`, `attachment <n>`. Limitaciones:

- **Tiene estado:** `trace open` extrae a `.playwright-cli/` en el **cwd** y se cierra con `trace close`. `behavioral.py evidence` ejecuta la CLI con **cwd dentro del directorio de evidencia** (nunca en el workspace `read_only`) y siempre cierra.
- **Sin salida JSON:** es texto (con ANSI en `errors`). Se entrega al agente de forma acotada, no se parsea de forma robusta.
- No probado: concurrencia de varios `trace open`, `trace request <id>`, `trace attachment <n>`.

Regla para `cc-resolve-comments`: empieza por el nivel 1; para subir de nivel debe indicar qué hipótesis intenta confirmar. Se aplica la regla existente de evidencia completa: una salida con marcador de truncado cuenta como incompleta si la conclusión depende de lo omitido.

## I. Integración con Playwright

**Versión mínima: `Playwright >= 1.63.0`** (todo el comportamiento descrito se comprobó en 1.63.0). No se afirma soporte de versiones anteriores. `behavioral.py doctor` además **sondea capacidades**: que `npx playwright trace --help` liste `actions` y `console`, y que `npx playwright test --help` liste `--forbid-only`, `--trace` y `--list`.

Comando de referencia (comprobado), ejecutado como **argv** sin shell y leyendo solo el JSON:

```bash
PLAYWRIGHT_JSON_OUTPUT_NAME=<run>/raw/playwright.json \
npx playwright test --reporter=line,json --retries=1 --trace=retain-on-first-failure \
  --output=<run>/artifacts --forbid-only --grep "<regex de selección>"
```

**Política de configuración (decidida en el Issue 2):** la `playwright.config` del proyecto es la autoridad; el toolkit **no la modifica ni genera una derivada** en el MVP. Solo aplica controles de ejecución por CLI (`--reporter`, `--trace`, `--retries`, `--output`, `--forbid-only`, `--grep`). `screenshot` y `video` siguen lo que configure el proyecto; son *best effort* y no se exigen para un PASS. Una config derivada temporal con defaults del toolkit (`screenshot: only-on-failure`, `video: off`) queda como mejora posterior.

`--reporter` por CLI **sustituye** los reporters de la config del proyecto (el HTML del proyecto se pierde salvo que se añada: `--reporter=line,json,html` con `PLAYWRIGHT_HTML_OPEN=never` y `PLAYWRIGHT_HTML_REPORT=<dir>`). `screenshot` y `video` **no tienen flag de CLI**: dependen de `playwright.config`.

Configuración recomendada para el proyecto (plantilla documentada; no se crea sin preguntar):

| Opción | Dónde | Local | CI | Motivo |
|---|---|---|---|---|
| reporter | CLI | `json` + `line` | `json` + `line` (+ `html` opcional) | El JSON es la fuente de verdad; HTML solo para humanos. |
| retries | CLI | 1 | 1 | Detecta flakiness y confirma repetición dentro del mismo run; **no** constituye reproducción independiente. Playwright marca `flaky` si el reintento pasa. |
| trace | CLI | `retain-on-first-failure` | igual | Conserva la traza del primer intento fallido, también con `retries=0` y en un flaky. `on-first-retry` no deja traza con `retries=0` y, en un flaky, solo la del intento que pasó. Coste: graba todas las ejecuciones y retiene solo la del primer fallo. |
| screenshot | **config** | `only-on-failure` | `only-on-failure` | Barato y útil; no es obligatorio para un PASS. |
| video | **config** | `off` | `on-first-retry` u `off` | Lo más caro en almacenamiento; opcional. |
| forbidOnly | CLI | **obligatorio** | **obligatorio** | Sin él, un `test.only` hace desaparecer los demás tests del JSON con exit 0. |
| workers | CLI/config | `min(4, cpus/2)` | 1-2 | Menos variabilidad en runners compartidos. |
| timeouts | config | test 30 s, expect 5 s | igual | Fallar rápido. |
| webServer / baseURL | config | `reuseExistingServer: true` (cc-run ya arrancó la app) | `false` | `BASE_URL` por variable de entorno. |

`error-context.md` se genera en cada intento fallido con independencia de `screenshot`, `video` y `trace`.

**Consideración operativa (no arquitectónica):** la primera ejecución en frío tardó 2,3 min en el entorno del spike; las siguientes, ~1 s. Conviene un timeout generoso en la primera llamada de un entorno nuevo.

### Reglas de PASS

El éxito se define por **JSON + selección + conteos**, no por el exit code. Para un test determinista:

```text
PASS =
  process exit == 0
  AND raw/playwright.json existe y es JSON válido
  AND selected_count >= 1
  AND executed selection == expected selection
  AND no unexpected
  AND no flaky
  AND no skipped/fixme dentro de la selección requerida
  AND no errors[] de nivel superior
  AND run.json persistido (con su hash)
  AND run.commit == HEAD
```

- `expected selection`: los tests que devuelve `npx playwright test --list --reporter=json` para el mismo patrón, antes de ejecutar.
- `executed selection`: los tests con al menos un resultado cuyo estado no es `skipped`. **El JSON no trae un campo "ejecutados"; se calcula.** Importa por `test.only`: sin `--forbid-only`, los demás tests desaparecen del JSON sin ser `skipped` y con exit 0.
- Exit 0 con cero tests ejecutados, con solo tests `skipped`, con `flaky`, con `--pass-with-no-tests` o con `--only-changed` sin cambios **no es un PASS**. No se usa `--pass-with-no-tests`.
- Un `flaky` no es PASS: limita la conclusión a `verified_with_reservations`. `--fail-on-flaky-tests` no se usa: cambia el exit code pero el JSON sigue diciendo `flaky`; decide el normalizador.
- Screenshot, trace y vídeo no son obligatorios para un PASS.

## J. Midscene (opcional, fase posterior)

No entra en el MVP. El contrato reserva `executionMode`:

| Valor | Cuándo | Peso |
|---|---|---|
| `deterministic` | Todo con `expect` y selectores | Pleno |
| `ai-assisted` | La IA localiza o actúa (`aiTap`, `aiAct`) pero los asserts son `expect` | Pleno; se registra si el caché dio hit |
| `ai-asserted` | Un `aiAssert`, `aiBoolean` o `aiQuery` decide el resultado | No basta por sí solo: conclusión máxima `verified_with_reservations`; exige conservar screenshot y reporte para auditar |

Reglas Playwright-first: IA solo cuando el selector o assert determinista es impracticable (localizar un elemento visual difícil, interfaces dinámicas, exploración, generación inicial, diagnóstico). `aiQuery`, `aiBoolean` y `aiAssert` con cautela. Desactivado por defecto (`ai.enabled: false`); activarlo implica enviar screenshots al proveedor de modelo y exige opt-in explícito. No hay puntuación de confianza: tres valores de `executionMode` bastan.

Limitación conocida: Midscene no soporta Claude para grounding; requiere un modelo de visión (Qwen-VL, Gemini, GPT, etc.) vía endpoint OpenAI-compatible. Sus aserciones y consultas AI no se cachean.

## K. Fix → rerun → regresión

| Paso | Regla |
|---|---|
| FAIL confirmado | El agente lee el nivel 1 y sube de nivel solo si lo necesita. |
| Tras el fix | Rerun solo del ID fallido. |
| Si pasa | Rerun de `@smoke`. No se infiere el "set afectado" desde el nombre del fichero ni el prefijo del ID: son heurísticas poco fiables (`AUTH-*` puede afectar a todo auth; `PATIENT-EDIT` y `PATIENT-DELETE` pueden no compartir nada). Opcional: tags de grupo explícitos (`@area:auth`, `@flow:login`) si el proyecto los adopta; `--only-changed` (comprobado: granularidad de fichero, sigue imports, funciona con árbol sucio y con ref) queda como **mejora futura validada**, con la salvedad de que "sin cambios" da exit 0 y cero tests. |
| Suite completa | Si el fix toca fixtures compartidos, auth, config de Playwright o dependencias (disparador crítico), o si la suite cabe en el presupuesto (p. ej. ≤ 5 min). |
| Test modificado | Si el fix toca los specs, `testsModified: true`. Si quita asserts, añade `skip/fixme` o sube timeouts, se marca "debilitado" y rereview debe verlo. |
| Volver a review | Sin regla extra: resolve ya pasa por rereview sobre el diff acumulado. |
| `ready` | La verificación behavioral alimenta el resultado de tests; no es una puerta aparte. El resultado de CI sobre el head es la fuente autoritativa. |

Flujo completo: `FAIL → evidencia → diagnóstico del agente → fix → rerun dirigido → regresión → PASS`.

## L. Taxonomía de fallos (pequeña)

Es una **pista heurística** del normalizador (`likelyCategory`), no un veredicto. Playwright no etiqueta las causas; el agente decide.

| Categoría | Señal | Estado del run |
|---|---|---|
| `assertion` | `expect` con valores distintos | `fail` |
| `selector` | timeout de locator o `strict mode violation` | `fail` |
| `timeout` | timeout de test o de acción | `fail` |
| `http` | respuesta 4xx/5xx inesperada (de `trace requests --failed` o del mensaje) | `fail` |
| `auth` | redirección inesperada a login, 401/403 | `fail` |
| `setup` | fallo en `beforeEach`, fixtures o `globalSetup`. **Heurística:** el JSON no marca los hooks (solo la ubicación del error); se **confirma con la traza** (`beforeEach hook ✗` en `trace actions`) | `error` |
| `environment` | `net::ERR_*` o `ECONNREFUSED` en cualquiera de los `errors[]` (un test con la app caída sale como `timedOut` con varias entradas), webServer caído (`errors[]` de nivel superior con conteos a 0), navegador sin arrancar. **Heurística:** no es distinguible de un fallo de producto solo por el estado; requiere **preflight de salud de `cc-run`** antes de ejecutar | `blocked` o `error` |
| `flaky` | el reintento pasó | `flaky` |

De SkyTest se adopta una regla, no su taxonomía F1-F10: un fallo real de aplicación nunca se arregla ajustando el test. Si el agente cambia el test, queda marcado (`testsModified`).

## M. `cc-doctor`

No existe. La versión mínima comprobada es 1.63.0 y el doctor **sondea capacidades**, no solo compara la versión. Propuesta: `scripts/behavioral.py doctor` ahora (salida legible y JSON), usado por `cc-verify` como preflight sin cambiar estado; más adelante puede envolverse en una skill `cc-doctor`.

```text
Behavioral verification
Playwright        ✓ 1.63.0 (>= 1.63.0)
Chromium          ✓ installed
Trace CLI         ✓ (probe: actions, console)
Test flags        ✓ (probe: --forbid-only, --trace, --list)
Config            ✓ code_cycle.behavioral (mode: auto)
Tests             ✓ 14 in tests/behavioral · 14 with ID · 0 duplicate IDs
baseURL           ✓ http://localhost:3000 (allowed origin)
Artifact dir      ✓ writable (…/evidence/behavioral)
CI                ✓ non-interactive (CI=true)
Midscene          ○ optional / not configured
AI provider       ○ not required
```

## N. Configuración

Sigue el patrón de `security_review.always_when`: una regla declarada reemplaza los defaults y una config ausente nunca apaga la puerta. Va como hermana de `security_review`, no bajo `verification` (ocupado).

```yaml
code_cycle:
  behavioral:
    mode: auto                    # auto | off   (como issue_review.mode)
    tests_dir: tests/behavioral   # etiqueta común @behavioral; ID por test con @AREA-NAME-NNN
    playwright_config: null       # null = descubrir playwright.config.*
    required_when:
      paths: ["src/ui/**", "app/**/*.tsx", "**/routes/**", "**/pages/**"]
    base_url: null                # normalmente se toma de lo que reporta cc-run (puertos distintos por worktree, #151); no fijar
    allowed_origins: [localhost, 127.0.0.1]
    retries: 1                    # se pasa por CLI (--retries); screenshot/video solo se fijan en playwright.config
    artifacts: { dir: null, keep_runs: 10 }
    ai: { enabled: false }        # Midscene, fase posterior
```

## O. Local, CI, retención y seguridad

**Local:** `cc-run` arranca la app → `behavioral.py run` → evidencia local.
**CI:** checkout → instalar → build y arrancar → `behavioral.py run` → subir artefactos → normalización. Sin pasos interactivos: `CI=true`, `--forbid-only` siempre.

| Destino | Qué entra |
|---|---|
| Git | `tests/behavioral/**/*.spec.ts`, helpers/fixtures, plantilla de config. **Nunca:** traces, vídeos, screenshots, `playwright.json`, `storageState`, `.env`. |
| Workspace temporal / local | Últimos 10 runs (`keep_runs`), limpieza acotada con dry-run por defecto. |
| Receipt / historial | Solo el bloque compacto de evidencia y los conteos. |
| Artifact de CI | `run.json` siempre; artefactos pesados solo si hay fallo; retención corta (p. ej. 14 días). |

Seguridad específica:

- Traces y screenshots contienen cookies, tokens y datos personales: directorio con permisos restrictivos; nunca se pegan en comentarios del PR (solo punteros y conteos).
- Los specs ejecutan código del repo con la misma confianza que la suite; siempre en workspace desechable.
- `allowed_origins` restringe los **destinos de navegación iniciados por el runner**. **No es aislamiento de red**: no impide que la aplicación o código de test haga `fetch`, XHR, iframes, websockets, imágenes o redirecciones a otros destinos, y los tests pueden ejecutar JavaScript arbitrario. Una protección real exigiría interceptación de requests (`route`), un contenedor/sandbox o política de red del runner; queda fuera del MVP y se documenta como tal.
- El JSON de Playwright incluye rutas absolutas del host; se relativizan antes de publicar nada.
- Credenciales solo por variables de entorno.
- Midscene envía screenshots al proveedor de modelo: opt-in explícito.

## P. MVP e issues propuestos (no creados)

**Alcance del MVP:** `cc-verify` ejecuta tests Playwright etiquetados, normaliza el resultado, el runtime sabe de forma determinista si era obligatorio, el agente recibe el nivel 1, corrige y se repite el rerun por ID y `@smoke`, todo con evidencia verificable.
**Fuera del MVP:** dashboard, scheduler, workers distribuidos, Midscene, columnas de telemetría más allá de `behavioral_status`, healing, visual regression, generación automática de tests, proveedores externos.

Seis issues, en orden:

### 1. Spike de capacidades de Playwright (#154) — COMPLETADO
Resultado: `docs/spikes/playwright-behavioral-capabilities.md` (verdict: GO WITH CHANGES, ya incorporado en la revisión 4).

- **Goal:** validar los supuestos marcados "a validar" antes de implementar.
- **Scope:** app de prueba mínima; JSON del reporter, `flaky` con retries, CLI `trace` (versión mínima, `console`), flags `--trace/--output/--forbid-only`, varios reporters por CLI, parseo de `Expected/Received`.
- **Out of scope:** código de producción.
- **Acceptance:** nota con versiones mínimas y comandos exactos.
- **Dependencies:** ninguna.

### 2. Behavioral core (#155) — IMPLEMENTADO en la PR que introduce este documento
Entregado: `scripts/behavioral.py` (subcomandos `list`, `run`, `normalize` para el camino de CI), `tests/test_behavioral.py` (62 tests) y fixtures reales del spike en `tests/fixtures/behavioral/`; añadido a `scripts/runtime.manifest`. Reglas añadidas durante la implementación: una selección que ejecuta tests que no estaban en el `--list` (grep colisionante) es `error` (`selection_exceeded`); un adjunto fuera del directorio de salida de Playwright nunca se copia a la evidencia; `npx --no-install` evita que se descargue Playwright en silencio; un fallo de entorno solo bloquea cuando el error dice `ECONNREFUSED`/`ERR_CONNECTION_REFUSED` y similares (no `ERR_ABORTED`). Validado de extremo a extremo contra Playwright 1.63.0 (pass, flaky, fail, skipped, ID desconocido y suite completa).

- **Goal:** ejecutar y normalizar, por una única ruta.
- **Scope:** `scripts/behavioral.py` con `list` (vía `--list --reporter=json`: IDs, duplicados, `covers`, selección esperada), `run` (selección por ID/tag con el grep `@ID(?![\w-])` como argv, `--forbid-only` siempre, `--trace=retain-on-first-failure`, reglas de PASS de la sección I, códigos de salida, `stability`, `fp` + `observed_signature`), `run.json`, `summary.md`, bundles y `raw/` con bloque `source`. Lee solo `raw/playwright.json`, nunca stdout; relativiza rutas absolutas; guarda el mensaje de error sin ANSI y extrae `Expected`/`Received` solo como best-effort (`null` si no se pueden).
- **Out of scope:** Midscene, selección de afectados, lector de evidencia.
- **Acceptance:** fixtures de reporter JSON (reutilizar los del spike) → salida esperada, incluidos cero tests (con y sin `--pass-with-no-tests`), flaky con exit 0, skip, `test.only` sin `--forbid-only` (selección ejecutada ≠ esperada → no pass), `forbidOnly`, error de `webServer` (→ `error/blocked`), grep sin colisión `-001`/`-0010`, mensaje con ANSI → texto limpio, `Expected/Received` ausente → `null` sin fallar; `fp` estable ante cambios no semánticos de línea; `fp` estable ante el cambio de síntoma 401 → 500 en la misma posición lógica; `observed_signature` distinta entre 401 y 500; `fp` distinto cuando cambia la posición lógica del fallo.
- **Dependencies:** 1.

### 3. Config y doctor (#156)
- **Goal:** configuración y preflight.
- **Scope:** claves `code_cycle.behavioral.*` con validación y docs (`docs/configuration.md`, `validate-package.py`); `behavioral.py doctor` con **sondeo de capacidades** (versión `>= 1.63.0`, `trace --help` lista `actions`/`console`, `test --help` lista `--forbid-only`/`--trace`/`--list`), Chromium instalado, tests descubiertos con ID y sin duplicados, directorio de evidencia escribible.
- **Out of scope:** skill `cc-doctor`.
- **Acceptance:** config ausente no rompe nada; una regla declarada reemplaza los defaults; salida y códigos de la sección M.
- **Dependencies:** 2 (para `doctor`).

### 4. Integración `cc-verify` y runtime (#157)
- **Goal:** `level: behavioral` con enforcement determinista.
- **Scope:** skill `cc-verify` y `docs/verification.md` (estados de la sección D); `EVIDENCE_LEVELS`; `behavioral_required_by_paths` en el runtime; columna `behavioral_status` (esquema 14) y mapeo de la sección D; fila `ui` en la tabla de boundary; tope por estado; `tests_basis`.
- **Out of scope:** resto de columnas de telemetría; `touches_ui`; `behavioral_verified` persistido.
- **Acceptance:** una obligación activada por regla no se puede quitar desde el agente; cada estado produce la conclusión de la sección D; `behavioral_verified` se deriva de `behavioral_status`; tests de `agent_reported`. Forma de `artifacts` alineada con #146.
- **Dependencies:** 2, 3; **#146** (forma del array `artifacts`).

### 5. Ciclo de review (#158)
- **Goal:** findings, fix loop y rerun.
- **Scope:** texto en review, rereview y resolve; línea de cuerpo con `fp`, `observed`, `confirmedByRun`; dedupe por `testId` + `fp`; `missing_behavioral_coverage`; regla del test debilitado; rerun por ID → `@smoke` → suite completa; todas las ejecuciones vía `behavioral.py`; regla de orquestación (despachar `targeted verify` en workspace desechable si falta un `BehavioralRun` del HEAD actual) en `run_cycle` y en ambos orquestadores; `cc-rereview` solo consume resultados y rechaza los stale (`commit != HEAD` o `dirty`).
- **Out of scope:** cambios en `review_contract.py`; que el runtime o los reviewers ejecuten Playwright.
- **Acceptance:** fixture de round-trip de comentario con la línea behavioral recuperada; no se abre REV nuevo por cambio de síntoma; un resultado de otro HEAD o con `dirty` no cierra el finding; si falta el run, el ciclo despacha el stage en vez de detenerse.
- **Dependencies:** 4. Desbloqueado por la decisión (a). Los reviewers heredan la regla de #147: solo se crea `missing_behavioral_coverage` frente a criterios escritos o la regla de rutas.

### 6. CI, retención y lector de evidencia (#159)
- **Goal:** operación fuera de la workstation y nivel 2 de disclosure.
- **Scope:** receta de GitHub Actions y subida de artifacts (camino a `externally_verified`); `keep_runs` con dry-run; `behavioral.py evidence` acotado: **primero `error-context.md`**, después la CLI de traces solo cuando haga falta, siempre con cwd en el directorio de evidencia y `trace close` al terminar.
- **Out of scope:** otros CI; parser propio de trazas.
- **Acceptance:** job `behavioral` no interactivo; la limpieza nunca borra el run en curso; salida truncada con marcador y totales.
- **Dependencies:** 2; coordinar con **#145** (clasificación de fallos de CI y un reintento): el rerun de #145 del job `behavioral` cuenta como confirmation run (`confirmedByRun`); no se reintenta dos veces el mismo fallo; documentar que `flaky` en #145 (causa transitoria) y en este diseño (fail→pass en el mismo run) no son lo mismo. Ruta de evidencia por ciclo (#141).

## Q. Decisiones explícitas

```text
Playwright role:    Sustrato determinista de ejecución; fuente de verdad (playwright.json, trace, screenshot)
Midscene role:      Capa AI opcional, desactivada por defecto, fase posterior; ai-asserted nunca certifica solo
Read-only policy:   Opción (a): ejecuta un stage verify/run disposable; review/rereview y runtime no ejecutan; rereview consume
                    un BehavioralRun del mismo HEAD (dirty=false); el runtime calcula required, despacha lo que falte y registra el estado
Evidence level:     level:"behavioral" propio; un behavioral pass satisface el boundary ui
Canonical owner:    Code Cycle Toolkit (scripts/behavioral.py + skills); los IDs de test viven en Git
Finding strategy:   REV-* con línea de cuerpo "behavioral: ID · fp · run"; sin VER-*; fp = posición lógica, síntoma aparte (observed_signature)
Receipt strategy:   Una entrada level:"behavioral" en VERIFICATION_RESULT (hash de run.json, commit, tool, config; artifacts al estilo #146);
                    una columna de telemetría behavioral_status (esquema 14); behavioral_verified derivado, behavioral_required calculado;
                    agent_reported; externally_verified vía el job de CI
Evidence strategy:  run.json + raw/playwright.json obligatorios (el normalizador lee solo el JSON, nunca stdout); error-context.md,
                    screenshot y trace (retain-on-first-failure) recomendados en fallo; vídeo opcional; console/network/DOM bajo demanda
                    desde la CLI de traces (con estado: cwd = directorio de evidencia). Nivel 2 empieza por error-context.md
PASS rule:          process exit == 0 AND JSON válido AND selected_count >= 1 AND executed selection == expected selection (vs --list)
                    AND sin unexpected/flaky/skipped/fixme en la selección AND sin errors[] de nivel superior AND run.json persistido
                    AND run.commit == HEAD; --forbid-only obligatorio
Playwright minimum: >= 1.63.0 (mínimo comprobado) con sondeo de capacidades en doctor; sin afirmar soporte anterior
Rerun strategy:     retry = attempt, no reproducción independiente; rerun por ID → @smoke → suite completa bajo disparadores concretos;
                    un run posterior (rereview o rerun de CI) es el confirmation run; tests editados quedan marcados
CI strategy:        Mismo script en CI, job "behavioral", artefactos solo en fallo; nada interactivo
MVP scope:          6 issues, con behavioral_required aplicado en runtime desde el inicio
Deferred:           Midscene, resto de telemetría (solo behavioral_status entra en el MVP), señal touches_ui, selección de tests afectados,
                    sospechosos de fix, histórico de flaky, VER-*, proveedores externos (SkyTest/Quorvex), skill cc-doctor
```

## Riesgos abiertos

- Validar en el spike: CLI de trace (versión mínima y `console`), `--only-changed`, parseo de `Expected/Received`, varios reporters por CLI.
- La razón `not_configured` quizá merezca token propio en vez de reutilizar `environment`.
- `tests_dir` y la config de Playwright del proyecto pueden chocar con convenciones existentes.
- Midscene no soporta Claude para grounding; esa capa dependerá de otro proveedor de visión.
