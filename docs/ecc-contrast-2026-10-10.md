# ECC frente a Code Cycle Toolkit: código, backlog y oportunidades

Contraste realizado el 10 de octubre de 2026 sobre el checkout `5ebec51` y las 50 issues abiertas de `datacas/code-cycle-toolkit`. Las cifras históricas citadas proceden de las issues; no son una nueva medición. Se revisaron código, contratos y documentación, sin ejecutar ciclos ni pruebas funcionales.

**La propuesta es acertada en dirección, pero presenta como nuevas varias capacidades que ya tenemos o están planificadas.** La oportunidad diferencial es Repository Knowledge & Learning: descubrir el conocimiento existente antes de implementar y aprender de los ciclos para mantener Markdown y procedimientos versionados en el repositorio. AgentMemory y otros índices son aceleradores reconstruibles, no la fuente de verdad.

Actualización tras la revisión del usuario: la primera versión limitaba demasiado el aprendizaje a findings corregidos y no fijaba almacenamiento versionado ni descubrimiento obligatorio. El [diseño de Repository Knowledge & Learning](repository-knowledge-learning-design.md) concreta estos requisitos, las fases, los permisos y los criterios de aceptación. Las mejoras de fiabilidad pueden avanzar en paralelo cuando no sean dependencias de la evidencia utilizada.

El MVP de Discovery incluye también Obligation Registry & Compliance Evidence: receipts por fase, obligaciones vinculadas a fuentes, cinco estados y validación independiente. Generar un dossier no demuestra cumplimiento. Los gates controlan entrada y conformidad sin impedir que implementación pendiente pase a review ni que una review termine con `CHANGES_REQUESTED`. Este contrato forma parte de Discovery, no de una iniciativa ni una base de datos adicionales.

El diseño precisa además extracción de Markdown con incertidumbre de aplicabilidad explícita, control exclusivo del registro por runtime y validación independiente de reducciones/exclusiones. La reutilización separa extracción, selección y evidencia: con entradas vigentes no añade dispatches de modelo por fase; las excepciones semánticas tienen presupuesto y el piloto mide también el coste dentro de las llamadas existentes.

## Qué cambiaría del diagnóstico original

| Idea | Qué existe en el toolkit | Issues abiertas relacionadas | Decisión propuesta |
| --- | --- | --- | --- |
| Aprendizaje continuo | Historial de findings dentro del ciclo, detección de supervivencia tras una corrección declarada y parada por repetición. No hay extracción y recuperación de reglas entre ciclos. | [#179](https://github.com/datacas/code-cycle-toolkit/issues/179) conserva el historial al reanudar; es un antecedente, no aprendizaje entre tareas. | Crear extracción y consolidación de candidatos desde implementación, pruebas, decisiones y correcciones; persistir en Git y controlar activación. |
| Recuperación iterativa | El prompt de cada etapa contiene referencias al trabajo, skill y evidencia; no vuelca automáticamente todo el historial. Las skills sí repiten bastante contenido. Behavioral ya diseña divulgación progresiva de evidencia. | [#184](https://github.com/datacas/code-cycle-toolkit/issues/184), [#144](https://github.com/datacas/code-cycle-toolkit/issues/144). | Mantener #184 sin perder reglas obligatorias; añadir descubrimiento local obligatorio antes de implementar y dossier por etapa. |
| Observabilidad | SQLite, intentos físicos, tokens, costes con procedencia, identidad del harness, snapshots de estado, actividad y seguimiento. | [#161–#169](https://github.com/datacas/code-cycle-toolkit/issues/161), [#180](https://github.com/datacas/code-cycle-toolkit/issues/180). | Completar la proyección y el monitor existentes; evitar un segundo almacén de estado. |
| Evaluación de agentes | `cc-stats`, calibración y resultados por ciclo/intento. La medición de precisión real o defectos escapados requiere evidencia adicional. | [#121–#129](https://github.com/datacas/code-cycle-toolkit/issues/121), especialmente [#126](https://github.com/datacas/code-cycle-toolkit/issues/126), [#127](https://github.com/datacas/code-cycle-toolkit/issues/127), [#128](https://github.com/datacas/code-cycle-toolkit/issues/128); [#181](https://github.com/datacas/code-cycle-toolkit/issues/181). | Ejecutar el roadmap de evaluación local; no crear otro framework genérico de evals. |
| Routing | Perfiles, reglas por dificultad/riesgo, disponibilidad, fallback, estrategias de coste y Jev en modo shadow. | [#175](https://github.com/datacas/code-cycle-toolkit/issues/175), #181 y #121–#129. | Evaluar y ajustar lo existente. No adoptar la heurística de ECC como selector nuevo. |
| Seguridad | Contratos de workspace por rol, revisión sin escritura, verificación en workspace desechable, controles de publicación y redacción de datos sensibles. | [#142](https://github.com/datacas/code-cycle-toolkit/issues/142), [#197](https://github.com/datacas/code-cycle-toolkit/issues/197), [#198](https://github.com/datacas/code-cycle-toolkit/issues/198), [#203](https://github.com/datacas/code-cycle-toolkit/issues/203). | Endurecer los límites actuales y evaluar auditoría opcional del harness. |
| Instalación modular | Instalación por host y scope; runtime opcional y manifiesto de módulos. No hay selección general de capacidades con plan de dependencias. | [#169](https://github.com/datacas/code-cycle-toolkit/issues/169) cubre integraciones del monitor. | Tomar el patrón planificar/aplicar; modularidad general tiene menor prioridad y excede #169. |
| Recuperación de sesiones | `--from`, `--continue`, detach, snapshots y recuperación de resultados omitidos mediante evidencia atribuida al intento. | [#179](https://github.com/datacas/code-cycle-toolkit/issues/179), [#201](https://github.com/datacas/code-cycle-toolkit/issues/201). | Corregir continuidad y reconciliación; no introducir otro gestor de sesiones. |

## Qué hemos comprobado en ECC

El [README enlazado](https://github.com/affaan-m/ECC/blob/main/docs/es/README.md) confirma las capacidades, pero conviene distinguir documentación, scripts operativos y el plano de control ECC2, descrito como alpha.

- [continuous-learning-v2](https://github.com/affaan-m/ECC/blob/main/skills/continuous-learning-v2/SKILL.md) define aprendizajes atómicos con disparador, acción, confianza, evidencia y scope. La versión documentada 2.1 añade aislamiento por proyecto. También contempla pérdida de confianza por contradicciones y promoción entre proyectos. Su [observador](https://github.com/affaan-m/ECC/blob/main/skills/continuous-learning-v2/agents/observer-loop.sh) incorpora protección contra reentrada, cooldown y muestreo de observaciones. Copiaría el modelo de evidencia y las protecciones, no el proceso de observación continua dependiente de Claude.
- [iterative-retrieval](https://github.com/affaan-m/ECC/blob/main/skills/iterative-retrieval/SKILL.md) es una guía con ejemplos de pseudocódigo: buscar, evaluar, refinar y repetir, con límite de tres rondas. No ofrece por sí sola un motor de recuperación incorporable. Tampoco copiaría mecánicamente exclusiones de tests o umbrales de relevancia: una revisión necesita pruebas y dependencias para entender el comportamiento.
- [model-route](https://github.com/affaan-m/ECC/blob/main/commands/model-route.md) es un comando de recomendación de tier según complejidad, riesgo y presupuesto. No demuestra selección calibrada con resultados locales; nuestro routing ya tiene más estructura ejecutable.
- El [state store](https://github.com/affaan-m/ECC/blob/main/scripts/lib/state-store/index.js) sí contiene implementación: SQLite mediante `sql.js`, migraciones, locking y protección de rutas. Sus [validadores](https://github.com/affaan-m/ECC/blob/main/scripts/lib/state-store/schema.js) separan entidades como sesiones, ejecuciones, decisiones, instalación y gobernanza. El patrón útil es separar contratos y proyecciones; trasladar su implementación añadiría otra pila a nuestro runtime Python.
- [install-plan.js](https://github.com/affaan-m/ECC/blob/main/scripts/install-plan.js) resuelve perfiles, componentes, skills e inclusiones/exclusiones sin modificar destinos. Esta separación entre plan y aplicación sí es una mejora práctica para nuestra distribución.

## 1. Repository Continuous Learning: conocimiento versionado más allá de findings

En [run_cycle.py](../scripts/run_cycle.py), el bucle calcula `claimed_fix_survivals(history)`, da instrucciones especiales cuando un finding sobrevive y detiene nuevas resoluciones tras dos supervivencias. También reconoce ausencia de progreso cuando coinciden head y findings abiertos. Esto es control del ciclo, no aprendizaje reutilizable.

[La issue #179](https://github.com/datacas/code-cycle-toolkit/issues/179) señala además que el historial se pierde entre ejecuciones reanudadas. Es una dependencia para consolidar automáticamente ese historial como evidencia completa. No bloquea descubrimiento local ni candidatos respaldados por evidencia íntegra de una única ejecución.

Propondría una nueva issue: **«Repository Continuous Learning: extracción y consolidación de conocimiento versionado»**. Alcance mínimo:

1. Capturar candidatos de implementación, tests, decisiones, incompatibilidades, documentación obsoleta y correcciones. Una observación no se convierte por ello en convención.
2. Conservar evidencia reproducible y su identidad compuesta: repositorio, tarea/PR, commit, intento y referencia al artefacto o finding, cuando exista. `REV-003` no identifica globalmente un problema.
3. Persistir Markdown en una ubicación configurada del repositorio; separar `candidate`, `validated`, `approved`, `deprecated` y `rejected`, así como vigencia y autoridad.
4. Consolidar duplicados y proponer actualizaciones de documentación existente antes de crear otro documento o skill.
5. Automatizar captura y preparación de cambios; controlar la activación normativa mediante aprobación o una autorización previa explícita para categorías de bajo riesgo. El revisor y la rereview permanecen sin escritura.

No equipararía repetición con confianza: tres rereviews del mismo fallo no son tres casos independientes. Tampoco una corrección declarada por el resolver demuestra que el patrón sea válido.

Git es la fuente de verdad del conocimiento persistente. AgentMemory puede indexar ese conocimiento de forma opcional, pero el descubrimiento local debe recuperar las reglas aprobadas sin él. Las reglas del workspace exigen guardar en AgentMemory por REST con el slug canónico del proyecto, evitando el MCP que pierde `project`. Este contraste no guarda memorias ni inventa un slug.

Hay otro límite relevante: [telemetry.md](telemetry.md) excluye narrativas de findings y texto de comentarios. Los aprendizajes no deberían colarse como nuevos campos de texto en telemetría; necesitan un contrato separado, con contenido derivado mínimo y referencias verificables.

## 2. Repository Knowledge Discovery: consultar antes de implementar

[compose()](../scripts/run_cycle.py) ya construye un prompt relativamente pequeño: rol, referencia al trabajo, instrucciones, límites de workspace y evidencia. Por tanto, el diagnóstico «enviamos todo a todos» no describe automáticamente nuestro runtime.

El problema concreto está documentado en [#184](https://github.com/datacas/code-cycle-toolkit/issues/184): las skills suman 333 KB y una resolución carga instrucciones propias y pases delegados repetidos. La issue persigue reducir un 40% el tamaño de las skills principales moviendo secciones a referencias bajo demanda. Debe preservar la carga de reglas obligatorias y comprobar su cobertura, además de reducir bytes. Este trabajo puede avanzar en paralelo al descubrimiento.

Propondría como primera capacidad nueva **«Repository Knowledge Discovery: descubrimiento obligatorio y dossier por etapa»**. Se ejecuta entre la comprensión del alcance y la planificación de implementación, incluso si se omite issue-review, se continúa una implementación o se invoca la skill directamente. Alcance:

- Descubrimiento determinista de instrucciones, documentación y skills existentes; no exige `.code-cycle/` ni escribe archivos del proyecto por defecto.
- Siempre presente: objetivo, restricciones, permisos, identidades, head/base, reglas aplicables y referencias a evidencia.
- Bajo demanda: símbolos, diff y tests relevantes, findings pendientes y antecedentes relacionados.
- Escalada: historial y logs específicos cuando exista una pregunta sin resolver.
- Límites explícitos de tamaño y rondas; registrar qué información falta y no confundir presupuesto agotado con contexto suficiente.
- Una búsqueda sin resultados relevantes permite continuar; una política explícitamente requerida que no puede leerse exige resolver el bloqueo. Relevancia no confiere autoridad.
- Actualizar el dossier cuando cambie materialmente el alcance o la versión de sus fuentes. El índice es una ayuda, nunca un sustituto de leer las políticas vigentes.
- Medir tokens por intento y por trabajo completado, duración y calidad de review; reducir bytes de Markdown no basta para demostrar ahorro real.

Serena y AgentMemory serían fuentes opcionales. Candidatos y antecedentes no se aplican como instrucciones. Las skills aprobadas del proyecto se distinguen de las skills operativas `cc-*`. El dossier no debe podar el diff acumulado que las revisiones están obligadas a cubrir. [#144](https://github.com/datacas/code-cycle-toolkit/issues/144) puede aportar reconocimiento previo de implementación; no reemplaza este contrato transversal.

## 3. Observabilidad: completar el monitor y distinguir actividad de progreso

[telemetry.py](../scripts/telemetry.py) está en schema 16. Ya registra intentos, consumo, variantes de ejecución, snapshots del harness y recuperaciones. [cycle_status.py](../scripts/cycle_status.py) ofrece lectura y seguimiento de snapshots. [#161](https://github.com/datacas/code-cycle-toolkit/issues/161) define su proyección normalizada, [#162](https://github.com/datacas/code-cycle-toolkit/issues/162) los eventos con cursor y [#163](https://github.com/datacas/code-cycle-toolkit/issues/163) CLI/watch. #164–#169 añaden providers, renderers e instalación.

La cuestión del polling también está resuelta como política en [cc-orca-orchestrator](../skills/cc-orca-orchestrator/SKILL.md): preferir notificaciones y usar polling adaptativo cuando no existan; su ausencia no debe detener el ciclo. Falta comprobar y ampliar la observación operativa, no descubrir ese principio en ECC.

Añadiría a #161/#162, o a un follow-up dependiente, un contrato explícito que diferencie:

| Señal | Qué permite afirmar |
| --- | --- |
| Heartbeat del supervisor | El supervisor responde. |
| Actividad de herramienta/proceso | Hay ejecución observada, aunque pueda estar repitiéndose. |
| Cambio verificable de etapa, evidencia, head o findings | Existe progreso hacia el resultado. |
| Silencio o fuente stale | Falta observación reciente; no demuestra por sí solo un bloqueo. |

Un watchdog debe diagnosticar antes de cancelar o redispatchar. Necesita ownership, reconciliación y evidencia terminal para evitar dos agentes escribiendo sobre la misma tarea. Las defensas del observador de ECC sirven como patrón, no como detector completo de estancamiento.

## 4. Evaluación y routing: el backlog ya contiene la estrategia

[router.py](../scripts/router.py) selecciona perfiles con señales declaradas: dificultad alta para implementación/resolución y riesgo de seguridad para review/rereview. [stats.py](../scripts/stats.py) calcula primer pase, rondas, findings repetidos, duración y otros resultados. [calibration_store.py](../scripts/calibration_store.py) ya soporta evidencia de calibración. Esto supera la propuesta genérica de asignar modelos por tipo de tarea.

Las issues [#126](https://github.com/datacas/code-cycle-toolkit/issues/126), [#127](https://github.com/datacas/code-cycle-toolkit/issues/127) y [#128](https://github.com/datacas/code-cycle-toolkit/issues/128) cubren evaluación controlada, evidencia local por cohort y recomendaciones aprobadas. Adoptar auto-routing que se reconfigura solo cambiaría ese objetivo supervisado.

Para el corto plazo, #175 propone bajar resolve a `high` y escalar tras supervivencia reiterada; #181 pide calibrar Sonnet antes de cambiar la ruta; #180 pide métricas del piloto. No atribuiría causalidad a la diferencia de duración entre modelos: los tamaños de muestra y los trabajos son distintos.

También corregiría dos expectativas del texto inicial:

- **Findings confirmados no equivale automáticamente a precisión del revisor.** Hace falta registrar cuáles fueron aceptados, refutados o reproducidos independientemente, con denominador claro.
- **Defectos escapados no se deduce de nuestros ciclos actuales.** Requiere vincular bugs posteriores con la PR original y una ventana de observación.

Añadiría esas métricas cuando exista una fuente fiable. El objetivo inmediato debe ser coste y tiempo por trabajo completado, con fallbacks, reanudaciones y calidad incluidos.

## 5. Behavioral, seguridad e instalación: continuar las piezas propias

**Behavioral no está integrado de extremo a extremo.** [behavioral.py](../scripts/behavioral.py) y su diseño ya aportan el núcleo y contratos de evidencia, pero siguen abiertas [#156](https://github.com/datacas/code-cycle-toolkit/issues/156) —config/doctor—, [#157](https://github.com/datacas/code-cycle-toolkit/issues/157) —verify/runtime—, [#158](https://github.com/datacas/code-cycle-toolkit/issues/158) —findings y fix loop— y [#159](https://github.com/datacas/code-cycle-toolkit/issues/159) —CI/retención/lector—. ECC no justifica duplicarlas; sí conviene terminar esa integración antes de vender aprendizaje de fallos de navegador.

**La separación de permisos ya existe.** [role-workspace-policy.md](role-workspace-policy.md) distingue escritura, solo lectura y ejecución desechable, con enforcement en cycle/executors. No basta para afirmar que todo el harness esté auditado frente a inyección, hooks o permisos MCP. AgentShield podría evaluarse como auditor externo opcional; primero resolvería #197, #198 y especialmente #203, que documenta un bloqueo real de publicación por metadatos Git en worktrees.

**La instalación ya es parcialmente modular.** [install.sh](../scripts/install.sh) selecciona host, scope y runtime opcional; [runtime.manifest](../scripts/runtime.manifest) enumera módulos. Faltan plan de capacidades, dependencias y diagnóstico general. #169 cubre solo integraciones del monitor, no toda la modularidad propuesta. Pospondría una issue general hasta que haya capacidades opcionales concretas que distribuir.

## Orden recomendado

1. **Dentro de Knowledge & Learning:** Discovery → Continuous Learning → Skill Promotion & Validation. Primero consultar lo existente, después generar candidatos y finalmente ampliar promoción, invalidación y evaluación de utilidad. El control mínimo de autoridad y vigencia debe existir desde Discovery.
2. **En paralelo, fiabilidad y continuidad:** #203, #201 y #179; completar #180 para medir el piloto. #179 condiciona la consolidación de evidencia entre reanudaciones, no todo el descubrimiento. La baseline publicada registra 21 paradas técnicas de 42 ciclos y solo 5 llegadas sin parada a `READY_FOR_MANUAL_MERGE`; debe actualizarse tras los fixes recientes.
3. **Reducir trabajo repetido:** #184, conservando políticas obligatorias, y [#182](https://github.com/datacas/code-cycle-toolkit/issues/182), que separa verificación dirigida en resolve del gate completo en rereview. Evaluar #175/#181 sin sacrificar independencia ni calidad de revisión.
4. **Continuar el backlog existente:** #161 → #162 → #163 para monitor; #156 → #157 → #158 → #159 para behavioral, respetando sus dependencias. No son prerequisitos globales para descubrir documentación.
5. **Optimizar con evidencia:** #126–#128; medir utilidad del conocimiento y coste por tarea completada. Instalación por capacidades y auditoría horizontal después, si el uso las justifica.

La contribución útil de ECC se concreta como **descubrir → aplicar → verificar → aprender → versionar → reutilizar**. El repositorio conserva el conocimiento; los índices son reconstruibles; la captura es automática y la activación de reglas está controlada. Propongo dos capacidades iniciales y una posterior de promoción/validación, sin duplicar telemetría, routing ni sesiones. Este documento no crea ni modifica issues y no implementa estas capacidades.
