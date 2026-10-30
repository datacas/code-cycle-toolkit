**Prioridad:** P0. **Esfuerzo:** M.

**Problema y justificación.** Si Orca interpreta perfiles y condiciones por separado, aparecerían dos políticas de ciclo con el mismo nombre.

**Objetivo y alcance.** Consumir el mismo plan, resolver y validadores en la orquestación supervisada de Orca.

**Componentes.** `cc-orca-orchestrator`, `OrcaDispatchContext`, `OrcaAdapter`, helpers de planificación y tests de instalación.

**Orientación técnica.** Conservar Run, Task, worker y receipts de Orca. El adapter consume esas identidades; no las inventa. Las decisiones de política vienen del componente común.

**Dependencia estricta.** #329.

**Criterios de aceptación:**

- Entradas equivalentes producen obligaciones equivalentes en Orca y nativos.
- El perfil efectivo y la revisión del plan viajan con la delegación.
- Un receipt de lanzamiento nunca se acepta como resultado funcional.
- Se mantienen workspaces de review y permisos de publicación.
- Cambios de HEAD y resultados parciales siguen las mismas reglas de invalidez.
- Los cinco modos tienen ejemplos y resultados terminales documentados.
- Versiones de Orca y limitaciones verificadas se registran en la evidencia.
- Rereview permanece condicional en todos los modos: sin modificaciones posteriores relevantes ni evidencia invalidada no se repite, tampoco en Full. Hallazgos abiertos sin correcciones mantienen sus bloqueos; Orca no lanza otra revisión idéntica para intentar otro resultado. Los cambios posteriores reactivan solo los controles cuya evidencia deba reevaluarse.

**Tests.** Fixtures equivalentes entre transportes; errores de contexto, lanzamiento sin finalización, resume, cancelación y al menos una campaña controlada con Orca real. Incluir paridad de selección/omisión de rereview en los cinco modos.

**Riesgos.** Suposiciones sobre capacidades no disponibles en la versión instalada.

**Fuera de alcance.** Sustituir Orca, crear un coordinador nuevo, renderers o protocolo remoto universal.

## Trazabilidad del roadmap aprobado

**Referencia estratégica:** [docs/ROADMAP_V2.md](https://github.com/datacas/code-cycle-toolkit/blob/35b9f9cdced8d5da549d9a84290a4b260243a0df/docs/ROADMAP_V2.md) (versión aprobada; la ruta canónica se conserva en el índice del repositorio).

**Bloqueos estrictos:** #329

Esta issue materializa la planificación aprobada el 26 de octubre de 2026; su existencia no acredita implementación. Las recomendaciones y condiciones no son bloqueos generales. No autoriza iniciar funcionalidades durante la materialización del roadmap.

<!-- cct-v2-approved-plan:2026-10-26 -->
