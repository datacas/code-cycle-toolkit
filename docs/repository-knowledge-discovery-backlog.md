# Repository Knowledge Discovery: entregas y trazabilidad

Issues publicadas el 10 de octubre de 2026 en `datacas/code-cycle-toolkit`: una de seguimiento y diez entregas verificables. Este backlog no implementa ni valida la funcionalidad.

**Seguimiento:** [#207](https://github.com/datacas/code-cycle-toolkit/issues/207). **Primeras entregas:** #208 (contrato de fuentes), seguido de #209 (contrato de obligaciones) y #211 (motor local), que pueden avanzar en paralelo después de #208.

| Entrega | Trabajo | Dependencias duras |
| --- | --- | --- |
| [D1 #208](https://github.com/datacas/code-cycle-toolkit/issues/208) | knowledge(contract): define versioned source, authority and applicability contracts | Ninguna |
| [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209) | knowledge(contract): define obligation, receipt and compliance evidence schemas | [D1 #208](https://github.com/datacas/code-cycle-toolkit/issues/208) |
| [D3 #210](https://github.com/datacas/code-cycle-toolkit/issues/210) | knowledge(registry): add runtime-owned revisioned obligation artifacts | [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209) |
| [D4 #211](https://github.com/datacas/code-cycle-toolkit/issues/211) | knowledge(discovery): add bounded local source discovery and coverage dossier | [D1 #208](https://github.com/datacas/code-cycle-toolkit/issues/208) |
| [D5 #212](https://github.com/datacas/code-cycle-toolkit/issues/212) | knowledge(extraction): structure normative instructions and retain uncertain applicability | [D1 #208](https://github.com/datacas/code-cycle-toolkit/issues/208), [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209), [D4 #211](https://github.com/datacas/code-cycle-toolkit/issues/211) |
| [D6 #213](https://github.com/datacas/code-cycle-toolkit/issues/213) | knowledge(receipts): cache discovery by source, scope and role with bounded cost | [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209), [D3 #210](https://github.com/datacas/code-cycle-toolkit/issues/210), [D4 #211](https://github.com/datacas/code-cycle-toolkit/issues/211), [D5 #212](https://github.com/datacas/code-cycle-toolkit/issues/212) |
| [D7 #214](https://github.com/datacas/code-cycle-toolkit/issues/214) | knowledge(skills): add shared discovery procedure to the four phase skills | [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209), [D4 #211](https://github.com/datacas/code-cycle-toolkit/issues/211), [D5 #212](https://github.com/datacas/code-cycle-toolkit/issues/212), [D6 #213](https://github.com/datacas/code-cycle-toolkit/issues/213) |
| [D9 #215](https://github.com/datacas/code-cycle-toolkit/issues/215) | knowledge(validation): verify compliance evidence and independent decisions | [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209), [D3 #210](https://github.com/datacas/code-cycle-toolkit/issues/210), [D5 #212](https://github.com/datacas/code-cycle-toolkit/issues/212) |
| [D8 #216](https://github.com/datacas/code-cycle-toolkit/issues/216) | knowledge(runtime): enforce four-phase entry and compliance gates without review deadlocks | [D3 #210](https://github.com/datacas/code-cycle-toolkit/issues/210), [D5 #212](https://github.com/datacas/code-cycle-toolkit/issues/212), [D6 #213](https://github.com/datacas/code-cycle-toolkit/issues/213), [D7 #214](https://github.com/datacas/code-cycle-toolkit/issues/214), [D9 #215](https://github.com/datacas/code-cycle-toolkit/issues/215) |
| [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) | knowledge(verification): run cross-phase regression fixtures and a measured discovery pilot | [D8 #216](https://github.com/datacas/code-cycle-toolkit/issues/216), [D9 #215](https://github.com/datacas/code-cycle-toolkit/issues/215) |

## Objective and boundary
Implement Repository Knowledge Discovery as small verifiable deliveries, not one issue combining retrieval, normative extraction, integrity, concurrency and four-phase orchestration.

The approved [design](repository-knowledge-learning-design.md) is preserved with this backlog. The tracking issue and its children were created before its publication and are self-contained. The documentation portion of [D1 #208](https://github.com/datacas/code-cycle-toolkit/issues/208) is covered here; executable contracts and tests remain pending. Design approval is not functional validation.

### Shared invariants
- Local repository files are authoritative; AgentMemory/Serena are optional. No mandatory target-repo directory or background observer.
- Runtime is the sole canonical registry writer. Workers propose operations; trusted independent controls confirm compliance.
- Applicability `undetermined` differs from compliance `unverified`; uncertainty cannot silently remove a requirement.
- Stable inputs add zero model dispatches, but existing-call tokens and reasoning overhead still count.
- Entry gates check current discovery/coverage; final conformity needs validated obligations. Pending implementation can reach review, and CHANGES_REQUESTED can reach resolution.
- Preserve workspace permissions, accumulated review, current recovery and manual merge. No new agent or database.

## Deliveries
- [ ] **[D1 #208](https://github.com/datacas/code-cycle-toolkit/issues/208)** knowledge(contract): define versioned source, authority and applicability contracts
- [ ] **[D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209)** knowledge(contract): define obligation, receipt and compliance evidence schemas
- [ ] **[D3 #210](https://github.com/datacas/code-cycle-toolkit/issues/210)** knowledge(registry): add runtime-owned revisioned obligation artifacts
- [ ] **[D4 #211](https://github.com/datacas/code-cycle-toolkit/issues/211)** knowledge(discovery): add bounded local source discovery and coverage dossier
- [ ] **[D5 #212](https://github.com/datacas/code-cycle-toolkit/issues/212)** knowledge(extraction): structure normative instructions and retain uncertain applicability
- [ ] **[D6 #213](https://github.com/datacas/code-cycle-toolkit/issues/213)** knowledge(receipts): cache discovery by source, scope and role with bounded cost
- [ ] **[D7 #214](https://github.com/datacas/code-cycle-toolkit/issues/214)** knowledge(skills): add shared discovery procedure to the four phase skills
- [ ] **[D9 #215](https://github.com/datacas/code-cycle-toolkit/issues/215)** knowledge(validation): verify compliance evidence and independent decisions
- [ ] **[D8 #216](https://github.com/datacas/code-cycle-toolkit/issues/216)** knowledge(runtime): enforce four-phase entry and compliance gates without review deadlocks
- [ ] **[D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217)** knowledge(verification): run cross-phase regression fixtures and a measured discovery pilot

## Dependencies and execution
- [D1 #208](https://github.com/datacas/code-cycle-toolkit/issues/208): can start independently
- [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209): [D1 #208](https://github.com/datacas/code-cycle-toolkit/issues/208)
- [D3 #210](https://github.com/datacas/code-cycle-toolkit/issues/210): [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209)
- [D4 #211](https://github.com/datacas/code-cycle-toolkit/issues/211): [D1 #208](https://github.com/datacas/code-cycle-toolkit/issues/208)
- [D5 #212](https://github.com/datacas/code-cycle-toolkit/issues/212): [D1 #208](https://github.com/datacas/code-cycle-toolkit/issues/208), [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209), [D4 #211](https://github.com/datacas/code-cycle-toolkit/issues/211)
- [D6 #213](https://github.com/datacas/code-cycle-toolkit/issues/213): [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209), [D3 #210](https://github.com/datacas/code-cycle-toolkit/issues/210), [D4 #211](https://github.com/datacas/code-cycle-toolkit/issues/211), [D5 #212](https://github.com/datacas/code-cycle-toolkit/issues/212)
- [D7 #214](https://github.com/datacas/code-cycle-toolkit/issues/214): [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209), [D4 #211](https://github.com/datacas/code-cycle-toolkit/issues/211), [D5 #212](https://github.com/datacas/code-cycle-toolkit/issues/212), [D6 #213](https://github.com/datacas/code-cycle-toolkit/issues/213)
- [D9 #215](https://github.com/datacas/code-cycle-toolkit/issues/215): [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209), [D3 #210](https://github.com/datacas/code-cycle-toolkit/issues/210), [D5 #212](https://github.com/datacas/code-cycle-toolkit/issues/212)
- [D8 #216](https://github.com/datacas/code-cycle-toolkit/issues/216): [D3 #210](https://github.com/datacas/code-cycle-toolkit/issues/210), [D5 #212](https://github.com/datacas/code-cycle-toolkit/issues/212), [D6 #213](https://github.com/datacas/code-cycle-toolkit/issues/213), [D7 #214](https://github.com/datacas/code-cycle-toolkit/issues/214), [D9 #215](https://github.com/datacas/code-cycle-toolkit/issues/215)
- [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217): [D8 #216](https://github.com/datacas/code-cycle-toolkit/issues/216), [D9 #215](https://github.com/datacas/code-cycle-toolkit/issues/215)

[D1 #208](https://github.com/datacas/code-cycle-toolkit/issues/208) → [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209) and [D4 #211](https://github.com/datacas/code-cycle-toolkit/issues/211); [D3 #210](https://github.com/datacas/code-cycle-toolkit/issues/210) and [D5 #212](https://github.com/datacas/code-cycle-toolkit/issues/212) can proceed when their contracts are ready; [D6 #213](https://github.com/datacas/code-cycle-toolkit/issues/213)/[D7 #214](https://github.com/datacas/code-cycle-toolkit/issues/214) then reuse them. [D9 #215](https://github.com/datacas/code-cycle-toolkit/issues/215) evidence validation is testable independently of runtime integration. [D8 #216](https://github.com/datacas/code-cycle-toolkit/issues/216) consumes [D9 #215](https://github.com/datacas/code-cycle-toolkit/issues/215), preventing a period where runtime gates trust self-asserted satisfied. [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) verifies the integrated workflow.
Intermediate deliveries expose tested components, not a complete validated capability.

## Traceability to the 23 approved acceptance criteria
| Criterion | Required behavior | Delivery |
| --- | --- | --- |
| CA-01 | Descubrir conocimiento del subsistema | [D1 #208](https://github.com/datacas/code-cycle-toolkit/issues/208), [D4 #211](https://github.com/datacas/code-cycle-toolkit/issues/211), [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) |
| CA-02 | Vacío legítimo frente a política requerida ilegible | [D1 #208](https://github.com/datacas/code-cycle-toolkit/issues/208), [D4 #211](https://github.com/datacas/code-cycle-toolkit/issues/211), [D8 #216](https://github.com/datacas/code-cycle-toolkit/issues/216), [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) |
| CA-03 | Fuentes locales sin backends; índices stale no activan reglas | [D1 #208](https://github.com/datacas/code-cycle-toolkit/issues/208), [D4 #211](https://github.com/datacas/code-cycle-toolkit/issues/211), [D6 #213](https://github.com/datacas/code-cycle-toolkit/issues/213), [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) |
| CA-04 | Reutilizar y refrescar dossier por fuentes/alcance | [D6 #213](https://github.com/datacas/code-cycle-toolkit/issues/213), [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) |
| CA-05 | Candidatos con procedencia; evidencia incompleta no valida | Posterior: Continuous Learning |
| CA-06 | Consolidación entre ciclos y observaciones independientes | Posterior: Continuous Learning |
| CA-07 | No activar conocimiento inválido/deprecado/contradicho | [D1 #208](https://github.com/datacas/code-cycle-toolkit/issues/208), [D4 #211](https://github.com/datacas/code-cycle-toolkit/issues/211), [D6 #213](https://github.com/datacas/code-cycle-toolkit/issues/213), [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) |
| CA-08 | Review sin escritura; validación atribuida al head | [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209), [D3 #210](https://github.com/datacas/code-cycle-toolkit/issues/210), [D7 #214](https://github.com/datacas/code-cycle-toolkit/issues/214), [D8 #216](https://github.com/datacas/code-cycle-toolkit/issues/216), [D9 #215](https://github.com/datacas/code-cycle-toolkit/issues/215), [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) |
| CA-09 | Cuatro fases, resume y garantías por modo/host | [D7 #214](https://github.com/datacas/code-cycle-toolkit/issues/214), [D8 #216](https://github.com/datacas/code-cycle-toolkit/issues/216), [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) |
| CA-10 | Inyección, límites de paths y autoridad | [D1 #208](https://github.com/datacas/code-cycle-toolkit/issues/208), [D4 #211](https://github.com/datacas/code-cycle-toolkit/issues/211), [D5 #212](https://github.com/datacas/code-cycle-toolkit/issues/212), [D8 #216](https://github.com/datacas/code-cycle-toolkit/issues/216), [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) |
| CA-11 | Piloto por tarea completada con cobertura/calidad | [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) |
| CA-12 | Dossier/autodeclaración no acreditan cumplimiento | [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209), [D8 #216](https://github.com/datacas/code-cycle-toolkit/issues/216), [D9 #215](https://github.com/datacas/code-cycle-toolkit/issues/215), [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) |
| CA-13 | Estados inconclusos/incumplidos bloquean conformidad | [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209), [D8 #216](https://github.com/datacas/code-cycle-toolkit/issues/216), [D9 #215](https://github.com/datacas/code-cycle-toolkit/issues/215), [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) |
| CA-14 | Alcance/fuentes/head invalidan reutilización injustificada | [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209), [D3 #210](https://github.com/datacas/code-cycle-toolkit/issues/210), [D6 #213](https://github.com/datacas/code-cycle-toolkit/issues/213), [D8 #216](https://github.com/datacas/code-cycle-toolkit/issues/216), [D9 #215](https://github.com/datacas/code-cycle-toolkit/issues/215), [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) |
| CA-15 | Pending llega a review; CHANGES_REQUESTED no causa deadlock | [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209), [D8 #216](https://github.com/datacas/code-cycle-toolkit/issues/216), [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) |
| CA-16 | Review general, reglas cualitativas y exclusiones | [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209), [D5 #212](https://github.com/datacas/code-cycle-toolkit/issues/212), [D7 #214](https://github.com/datacas/code-cycle-toolkit/issues/214), [D9 #215](https://github.com/datacas/code-cycle-toolkit/issues/215), [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) |
| CA-17 | Conservar reglas y resultados históricos | [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209), [D3 #210](https://github.com/datacas/code-cycle-toolkit/issues/210), [D9 #215](https://github.com/datacas/code-cycle-toolkit/issues/215), [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) |
| CA-18 | Cobertura normativa y aplicabilidad undetermined | [D1 #208](https://github.com/datacas/code-cycle-toolkit/issues/208), [D5 #212](https://github.com/datacas/code-cycle-toolkit/issues/212), [D7 #214](https://github.com/datacas/code-cycle-toolkit/issues/214), [D8 #216](https://github.com/datacas/code-cycle-toolkit/issues/216), [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) |
| CA-19 | Sin eliminación/rebaja unilateral; actor y concurrencia | [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209), [D3 #210](https://github.com/datacas/code-cycle-toolkit/issues/210), [D8 #216](https://github.com/datacas/code-cycle-toolkit/issues/216), [D9 #215](https://github.com/datacas/code-cycle-toolkit/issues/215), [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) |
| CA-20 | Corrección/duplicado trazable y autoridad normativa | [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209), [D3 #210](https://github.com/datacas/code-cycle-toolkit/issues/210), [D8 #216](https://github.com/datacas/code-cycle-toolkit/issues/216), [D9 #215](https://github.com/datacas/code-cycle-toolkit/issues/215), [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) |
| CA-21 | Cero dispatches adicionales con entradas estables | [D6 #213](https://github.com/datacas/code-cycle-toolkit/issues/213), [D7 #214](https://github.com/datacas/code-cycle-toolkit/issues/214), [D8 #216](https://github.com/datacas/code-cycle-toolkit/issues/216), [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) |
| CA-22 | Cache por capas; revalidar evidencia tras cambios | [D6 #213](https://github.com/datacas/code-cycle-toolkit/issues/213), [D9 #215](https://github.com/datacas/code-cycle-toolkit/issues/215), [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) |
| CA-23 | Presupuestos finitos y coste dentro de llamadas existentes | [D6 #213](https://github.com/datacas/code-cycle-toolkit/issues/213), [D10 #217](https://github.com/datacas/code-cycle-toolkit/issues/217) |

## Existing backlog and follow-ups
- #144 remains the implementation code reconnaissance task, with its own no-new-skill constraint. [D4 #211](https://github.com/datacas/code-cycle-toolkit/issues/211)/[D7 #214](https://github.com/datacas/code-cycle-toolkit/issues/214) consume or align its brief rather than duplicate it. The shared four-role discovery skill belongs to this capability.
- #184 can proceed in parallel; moving instructions to references must preserve mandatory loading.
- #179 is needed for complete cross-resume historical learning evidence, not as a global dependency for local discovery.
- Monitoring, model routing and behavioral verification keep their existing issues; this plan does not recreate them.
- **Deferred:** Continuous Learning capture/persistence and cross-cycle consolidation (CA-05/06), then skill promotion/advanced validation. They reuse the source/obligation/evidence contracts and need their own small implementation issues.

## Tracking acceptance
Every delivery has executable fixtures or observed integration checks, explicit dependencies and boundaries. All criteria have an owner or an explicit deferral; closure of this Discovery tracker does not claim CA-05/06 or learning implementation. Pilot and unsupported-host results must state actual evidence and limits.

## D1: knowledge(contract): define versioned source, authority and applicability contracts

**Issue:** [#208](https://github.com/datacas/code-cycle-toolkit/issues/208). **Bloque:** Contratos. **Dependencias:** ninguna. **Criterios:** CA-01, CA-02, CA-03, CA-07, CA-10, CA-18.

### Problem and result
Discovery needs a common representation of repository sources before any phase can trust its output. Define executable, versioned contracts for source identity, authority, applicability and coverage; existing Markdown remains usable without migration.

### Scope
- Add a focused contract module (proposed `scripts/knowledge_contract.py`). The approved design documentation is preserved alongside this backlog; executable contracts remain to be implemented.
- Source records include repo identity, path/section, observed content hash, source category/authority, scope and origin. Model instruction precedence without making toolkit defaults superior to authorized project instructions.
- Applicability is `applicable|not_applicable|undetermined`, separate from compliance. Preserve conditions and observed facts; confidence never grants authority.
- Define discovery outcomes distinguishing legitimate no-results, required-source read failure, unresolved conflict and optional-backend degradation.
- Accept legacy documents without metadata; parse generated metadata without activating candidate, rejected, deprecated or invalidated knowledge as rules.

### Acceptance and verification
- Fixture tests accept documented valid records and reject malformed/unknown schema versions with explicit errors.
- Required policy, approved procedure, validated context and candidate remain distinct; examples and issue text cannot grant permissions.
- Fixtures distinguish unavailable required policy from no relevant knowledge and undetermined applicability from attempted-but-inconclusive verification.
- All contracts work without AgentMemory/Serena and require neither `.code-cycle/` nor writes to a target checkout.

### Boundary
No retrieval engine, registry writer, LLM dispatch, learning extraction or runtime gate in this issue.

## D2: knowledge(contract): define obligation, receipt and compliance evidence schemas

**Issue:** [#209](https://github.com/datacas/code-cycle-toolkit/issues/209). **Bloque:** Contratos. **Dependencias:** [D1 #208](https://github.com/datacas/code-cycle-toolkit/issues/208). **Criterios:** CA-08, CA-12, CA-13, CA-14, CA-15, CA-16, CA-17, CA-19, CA-20.

### Problem and result
A context dossier cannot prove compliance. Define the structured boundary shared by implement, initial-review, resolve and rereview before wiring a writer or gate.

### Scope
- Extend the knowledge contract with stable obligation IDs and explicit revisions, source references, normative weight, applicability, expected result and admissible check.
- Compliance states: `pending|satisfied|violated|not_applicable|unverified`. Keep these separate from applicability and knowledge lifecycle.
- Separate executor-proposed state from validator-confirmed state, with actor attribution, control identity, evidence, observed result and evaluated commit/tree.
- Define per-phase receipts and registry-delta requests with work/cycle/attempt identity, role, scope, source inventory fingerprints, registry revision and `expected_revision`.
- Specify outcomes for completed execution, pending independent validation, product nonconformance and blocked prerequisites. Do not silently redefine existing runtime enums.

### Acceptance and verification
- Contract fixtures reject self-asserted confirmation, receipts for another role/work/attempt and evidence missing evaluated-state attribution.
- A qualitative independent assessment and an authorized exclusion have explicit evidence/authority fields.
- History references are immutable; a future policy/learning revision cannot replace the obligation revision of a past evaluation.
- Fixtures model implement ready for review with pending compliance and review ending in CHANGES_REQUESTED without claiming product conformity.

### Boundary
Schema validation alone does not establish actor trust, isolate storage or verify that a test actually covers an obligation; those are subsequent deliveries.

## D3: knowledge(registry): add runtime-owned revisioned obligation artifacts

**Issue:** [#210](https://github.com/datacas/code-cycle-toolkit/issues/210). **Bloque:** Motor local. **Dependencias:** [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209). **Criterios:** CA-08, CA-14, CA-17, CA-19, CA-20.

### Problem and result
A writer must not remove an inconvenient obligation or replace canonical state. Implement a runtime-owned structured-artifact store and validated operations; do not add another database.

### Scope
- Add a focused registry module (proposed `scripts/knowledge_registry.py`) using D2 contracts, atomic persistence and retained revision/event history.
- The runtime is the canonical writer. Worker outputs are proposals; derive actor/role from the dispatch boundary rather than trusting a payload field.
- Enforce expected revision and safe reconciliation of concurrent updates.
- Implement authorization for additions, correction proposals, source changes, exclusions and weakening. Implement/resolve cannot authorize their own weakening, exclusion or satisfied state.
- Preserve duplicates/corrected extractions with links and reasons rather than deleting history.
- Define artifact ownership, retention and location outside worker-writable roots; expose adapter isolation requirements. A hash is not a write permission boundary.

### Acceptance and verification
- Two updates from one revision cannot both silently win; restart/replay retains a consistent canonical revision.
- Tests reject replacement, silent omission, forged actor, unilateral downgrade and history mutation.
- A source-policy edit from a writing stage produces a pending policy delta rather than automatically adopting a more permissive rule.
- Independent reviewers may submit results without writing to the checkout; unsupported isolation is surfaced and cannot advertise strong enforcement.

### Boundary
No policy interpretation, retrieval, new model call or target-repo knowledge writing. Live adapter enforcement is wired in the runtime integration delivery.

## D4: knowledge(discovery): add bounded local source discovery and coverage dossier

**Issue:** [#211](https://github.com/datacas/code-cycle-toolkit/issues/211). **Bloque:** Motor local. **Dependencias:** [D1 #208](https://github.com/datacas/code-cycle-toolkit/issues/208). **Criterios:** CA-01, CA-02, CA-03, CA-07, CA-10.

### Problem and result
Agents must consult existing repository knowledge even when no generated knowledge directory exists. Implement a deterministic, read-only local inventory and selection engine.

### Scope
- Add a focused discovery module (proposed `scripts/knowledge_discovery.py`) with role, work scope and checkout inputs.
- Resolve applicable AGENTS/CLAUDE/CONTRIBUTING instructions and explicit references, including host-supplied ancestor instructions and directory scope. Preserve precedence.
- Discover configured docs/skills plus existing repository conventions; select by paths, language, responsibility and topics without title-only matching.
- Read selected sources and emit a bounded dossier with versions, coverage, conflicts, gaps and meaningful discards.
- Enforce authorized read boundaries and safe path/symlink handling; avoid secrets and generated trees.
- Mandatory policies are not discarded for ranking or size budgets. Optional semantic/symbol backends are not required.

### Acceptance and verification
- A fixture task affecting src/events finds subsystem documentation and approved procedures without a finding or external service.
- Empty relevant knowledge continues explicitly; a required unreadable reference yields a distinct blocking reason.
- Candidate/deprecated/invalidated procedures are not activated; unauthorized paths and embedded malicious instructions cannot grant capabilities.
- The target checkout remains untouched, including when `.code-cycle/` is absent.

### Boundary
No automatic conversion of arbitrary prose to proven obligations, no canonical writer or per-phase dispatch. Coordinate with #144 reconnaissance, without reimplementing its code scout.

## D5: knowledge(extraction): structure normative instructions and retain uncertain applicability

**Issue:** [#212](https://github.com/datacas/code-cycle-toolkit/issues/212). **Bloque:** Motor local. **Dependencias:** [D1 #208](https://github.com/datacas/code-cycle-toolkit/issues/208), [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209), [D4 #211](https://github.com/datacas/code-cycle-toolkit/issues/211). **Criterios:** CA-10, CA-16, CA-18.

### Problem and result
Finding Markdown is not extracting its rules. Implement normalization of structured rules and a bounded proposal/coverage interface for free prose, without pretending deterministic code understands all instructions.

### Scope
- Add a focused obligation extraction module (proposed `scripts/knowledge_obligations.py`).
- Normalize structured approved procedures and formalized conditions locally; preserve provenance and map normative fragments to obligations, linked duplicates or unresolved interpretation.
- Accept semantic interpretation proposals produced within existing phase work; label them unvalidated until an authorized check confirms them.
- Preserve action, applicability condition, expected result and admissible validation method; never invent commands or tests absent from sources.
- Separate `undetermined` applicability from `unverified` compliance. Qualitative rules use reasoned independent inspection.
- Expose unresolved mandatory-action dependencies to the gate; keep candidates/context out of normative extraction.

### Acceptance and verification
- Fixtures containing rules, examples and recommendations produce an auditable coverage map rather than treating every sentence as mandatory.
- Structured path/version conditions resolve from observed facts; material ambiguous restrictions remain represented and cannot vanish through a low confidence score.
- Applicable qualitative obligations can proceed toward review without an artificial test; unresolved restrictions cannot authorize their dependent action.
- Parse/proposal validation uses no added model dispatch. Tests exercise missing/contradictory conditions and unsupported interpretation.

### Boundary
No learning or generated tests. Reuse of extraction is implemented separately; runtime handling of ambiguity uses existing authorization and structured stop semantics.

## D6: knowledge(receipts): cache discovery by source, scope and role with bounded cost

**Issue:** [#213](https://github.com/datacas/code-cycle-toolkit/issues/213). **Bloque:** Motor local. **Dependencias:** [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209), [D3 #210](https://github.com/datacas/code-cycle-toolkit/issues/210), [D4 #211](https://github.com/datacas/code-cycle-toolkit/issues/211), [D5 #212](https://github.com/datacas/code-cycle-toolkit/issues/212). **Criterios:** CA-03, CA-04, CA-07, CA-14, CA-21, CA-22, CA-23.

### Problem and result
Repeating discovery and model summaries in every phase would undo context savings. Implement reusable local extraction/selection artifacts and phase-specific receipts without reusing stale compliance.

### Scope
- Add a focused receipt/cache module (proposed `scripts/knowledge_receipts.py`).
- Separate source extraction, role/scope selection and receipt/evidence layers. Keys include repo/authority isolation, source inventory/hashes, method/schema, applicability facts and phase contract.
- Recheck relevant inventory including new/deleted policies, uncommitted content, scope/config changes. TTL or unchanged head alone cannot establish validity.
- Issue each phase its own runtime-attributed receipt. Reuse prior extraction without treating an unvalidated proposal as approved.
- Code changes may preserve knowledge extraction while invalidating affected compliance evidence.
- Expose content-free cache hit/miss reasons, local duration, delivered volume and additional-dispatch counters. Default budget is zero extra model dispatches; exceptional semantic steps need finite configured limits.

### Acceptance and verification
- Stable inputs across four phase roles reuse extraction and perform no extra model dispatch; receipts remain role/work/attempt specific.
- Tests invalidate only affected layers for new policy, uncommitted edit, scope expansion, changed authority/method and code-only changes.
- Missing facts produce miss/uncertainty, never a fabricated hit; optional backend outage uses local inputs.
- Exhausted budgets preserve unresolved status without silent omission or unbounded retries. Unattributable usage stays unknown.
- No document bodies or finding narratives enter telemetry.

### Boundary
Integrate into existing metrics/manifest conventions; do not create new monitoring/routing infrastructure or another canonical knowledge store.

## D7: knowledge(skills): add shared discovery procedure to the four phase skills

**Issue:** [#214](https://github.com/datacas/code-cycle-toolkit/issues/214). **Bloque:** Integración. **Dependencias:** [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209), [D4 #211](https://github.com/datacas/code-cycle-toolkit/issues/211), [D5 #212](https://github.com/datacas/code-cycle-toolkit/issues/212), [D6 #213](https://github.com/datacas/code-cycle-toolkit/issues/213). **Criterios:** CA-08, CA-09, CA-16, CA-18, CA-21.

### Problem and result
Runtime integration alone leaves standalone skills with a different workflow. Add the common `cc-knowledge-discovery` skill and use the same phase-specific procedure and obligations in all four skills.

### Scope
- Create the shared skill, supporting on-demand references and package/installer registration following repository conventions.
- Update cc-implement-issue, cc-initial-review, cc-resolve-comments and cc-rereview to invoke it or validate a current role receipt before substantive work.
- Require implement/resolve to plan compliance and supply evidence; reviewers check source coverage, applicability and evidence independently while retaining general accumulated review.
- Reuse valid context and add only role-specific gaps. Do not copy the entire knowledge library into skills.
- Preserve read-only roles, language/provider contracts, current machine-readable tokens and required policy loading.
- Document the difference between supervised runtime/wrapper enforcement and procedural standalone execution.

### Acceptance and verification
- Skill contract tests cover all four roles, direct invocation, no-results, unresolved required policy and reuse.
- Package/manifest/installation tests include the new skill for Claude Code, Codex and OpenCode without a required host hook or memory service.
- Review fixtures detect an undocumented defect outside the obligation list.
- Existing verification and accumulated-diff obligations are unchanged.

### Boundary
No new agent. Coordinate with #184 shared-reference slimming and #144 existing scout; #144's no-new-skill constraint remains scoped to that scout issue, not this shared four-phase capability.

## D9: knowledge(validation): verify compliance evidence and independent decisions

**Issue:** [#215](https://github.com/datacas/code-cycle-toolkit/issues/215). **Bloque:** Validación. **Dependencias:** [D2 #209](https://github.com/datacas/code-cycle-toolkit/issues/209), [D3 #210](https://github.com/datacas/code-cycle-toolkit/issues/210), [D5 #212](https://github.com/datacas/code-cycle-toolkit/issues/212). **Criterios:** CA-08, CA-12, CA-13, CA-14, CA-16, CA-17, CA-19, CA-20, CA-22.

### Problem and result
Self-declared satisfied is not compliance. Implement evidence verification independently of phase dispatch, so gates can consume confirmed conclusions rather than labels.

### Scope
- Add a focused verifier (proposed `scripts/knowledge_compliance.py`) consuming D2 evidence and trusted runtime actor context.
- Verify work/obligation revision, evaluated head/tree, referenced artifacts, observed check result and the scope actually covered by each control.
- Confirm only conclusions supported by a deterministic check or an authorized independent review. An agent-run test can supply evidence but is not blanket proof.
- Support reasoned qualitative review, validated non-applicability and explicit authorized exceptions without confusing them.
- Retain inconclusive and violation results, and invalidate code/dependency-affected evidence on a new state.
- Reconcile results as versioned registry operations without writing the reviewed checkout.

### Acceptance and verification
- Tests reject fabricated/missing evidence, stale head, mismatched obligation revision, self-confirmation and an exclusion outside validator authority.
- Tests preserve pending/violated/unverified distinctly; qualitative evidence is assessed with source/code reasoning.
- A test proving one invariant cannot satisfy unrelated obligations.
- Policy edits or later learning cannot rewrite an earlier validation; exceptions retain authority, scope and reason.
- Verification uses local checks or existing reviewers, with no new automatic reviewer dispatch.

### Boundary
Do not generate tests from prose or run a separate agent. Runtime gates and real executor enforcement belong to D8; this verifier is directly testable through contract fixtures.

## D8: knowledge(runtime): enforce four-phase entry and compliance gates without review deadlocks

**Issue:** [#216](https://github.com/datacas/code-cycle-toolkit/issues/216). **Bloque:** Integración. **Dependencias:** [D3 #210](https://github.com/datacas/code-cycle-toolkit/issues/210), [D5 #212](https://github.com/datacas/code-cycle-toolkit/issues/212), [D6 #213](https://github.com/datacas/code-cycle-toolkit/issues/213), [D7 #214](https://github.com/datacas/code-cycle-toolkit/issues/214), [D9 #215](https://github.com/datacas/code-cycle-toolkit/issues/215). **Criterios:** CA-02, CA-08, CA-09, CA-10, CA-12, CA-13, CA-14, CA-15, CA-18, CA-19, CA-20, CA-21.

### Problem and result
Prompts cannot guarantee knowledge consultation. Wire local Discovery, receipts, protected registry and confirmed evidence into the runtime's phase boundaries without preventing the review needed to validate implementation.

### Scope
- Integrate at run_cycle/compose/CycleRecorder/executor contracts; preserve routing, fallback, structured result recovery and workspace rules.
- Check role-specific current receipt, coverage and mandatory unresolved conflicts before substantive implement/review/resolve/rereview work, including issue-review skipped/off, --continue and resumed --from.
- Permit bounded read-only clarification preparation for unknown scope/prose, without authorizing dependent edits. Document the admission handshake and supported enforcement surfaces.
- Validate registry operations and D9 conclusions after execution. Distinguish pending independent validation from process completion and product conformity.
- Protect canonical artifact roots with effective adapter boundaries; unsupported adapters cannot silently claim strong enforcement.
- Keep feature activation explicit during staged rollout; intermediate foundations alone must not advertise a validated complete capability.

### Acceptance and verification
- Runtime tests prevent dependent actions on stale/missing receipt or unresolved required policy, and do not block legitimate empty discovery.
- Implement with pending review validation reaches initial-review; CHANGES_REQUESTED reaches resolve; no circular wait for a reviewer the gate prevents dispatching.
- Mandatory pending/violated/unverified cannot produce READY_FOR_MANUAL_MERGE; safe structured failures/blocks remain reportable.
- Resume and fallback cannot substitute receipts, lose canonical revisions or grant reviewer writes. Modified policy cannot silently weaken its own evaluation.
- Stable knowledge adds no model dispatch and invocation mode limitations are explicit.

### Boundary
No new agent, merge, optional-hook framework, learning extraction or routing redesign. #179 is related historical finding recovery, not a dependency for local Discovery; preserve existing history behavior and label unavailable evidence honestly.

## D10: knowledge(verification): run cross-phase regression fixtures and a measured discovery pilot

**Issue:** [#217](https://github.com/datacas/code-cycle-toolkit/issues/217). **Bloque:** Validación. **Dependencias:** [D8 #216](https://github.com/datacas/code-cycle-toolkit/issues/216), [D9 #215](https://github.com/datacas/code-cycle-toolkit/issues/215). **Criterios:** CA-01, CA-02, CA-03, CA-04, CA-07, CA-08, CA-09, CA-10, CA-11, CA-12, CA-13, CA-14, CA-15, CA-16, CA-17, CA-18, CA-19, CA-20, CA-21, CA-22, CA-23.

### Problem and result
Passing unit contracts does not demonstrate that the four-phase workflow consults and validates knowledge effectively. Verify the integrated capability and report its cost/quality limits.

### Scope
- Build a cross-phase fixture matrix reusing D1-D9 tests rather than duplicating implementations.
- Exercise normal, no-knowledge, required-policy unreadable, malicious source, ambiguous applicability, stale/new source, code-only change, policy weakening, concurrency, resume and fallback paths.
- Exercise supported Claude Code/Codex/OpenCode boundaries; use deterministic fake executors for reproducible checks and report real-surface probes separately as executed or unverified.
- Count the planned four-phase dispatches and added dispatches, cache reuse and delivered context. Measure existing-call tokens/duration where actually available.
- Run a small approved pilot on comparable work using existing telemetry/calibration conventions; report coverage, review rounds, repeated failures, completed-work time/cost and missing measurements.

### Acceptance and verification
- The adversarial matrix cannot close nonconformant work or mutate review workspaces, and cannot hide unknown applicability/evidence.
- Stable inputs demonstrate zero additional model dispatches, without claiming zero token/latency overhead.
- Review still detects defects outside the registry; policies/knowledge revisions preserve historic results.
- Pilot methodology and observed results are reproducible; unsupported surfaces and insufficient sample sizes are explicit.
- No release claim of complete validated Discovery until critical integration checks pass; a pilot showing regression reports it rather than asserting improvement.

### Boundary
No new evaluation platform or host installation system. Criteria 5 and 6 (candidate learning and cross-cycle consolidation) remain deferred to Continuous Learning and are not claimed as completed here.

## Estado documental y alcance

Las issues se crearon antes de publicar el diseño y son autocontenidas. Esta documentación conserva el diseño aprobado y cubre la parte documental de #208, sin completar sus contratos ni cerrar la issue. #144, #179 y #184 mantienen su alcance; se documentan relaciones, no dependencias globales inventadas. CA-05 y CA-06 quedan explícitamente diferidos a Continuous Learning; la promoción avanzada de skills es posterior. El resto tiene trazabilidad a entregas y verificación integrada en #217.
