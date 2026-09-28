# General tool and documentation routing

- For project source code, symbols, references, implementations, and edits, use Serena first when available.
- For any external library, framework, SDK, API, CLI, protocol, database, platform, or third-party service, use Context7 before relying on model memory when Context7 is available.
- Prefer documentation that matches the exact version used by the project.
- Use Context7 especially for:
  - version-sensitive APIs
  - configuration
  - command-line syntax
  - deprecations
  - migrations
  - integration details
  - supported features
  - breaking changes
  - exact method signatures or options
- Do not assume an API, option, command, configuration key, or behavior exists solely from prior model knowledge when current documentation can be checked.
- If Context7 does not contain the dependency, is unavailable, or returns incomplete information, fall back to official vendor documentation or another authoritative source.
- For current product behavior, release information, vendor-specific features, or documentation not indexed by Context7, prefer official documentation or authoritative web sources.
- Do not use Context7 for the project's own source code; use repository navigation tools such as Serena instead.
- Do not query Context7 for generic programming knowledge that is stable and not version-sensitive.
- Serena and Context7 are optional capabilities. Their absence or failure must never block the workflow.
- If a preferred tool is unavailable, continue using the best available fallback.
- When documentation and project code disagree, treat the project's installed version and actual runtime behavior as authoritative, and use documentation to explain the discrepancy.
- Verify uncertain or version-sensitive claims before modifying code based on them.
- Avoid unnecessary documentation lookups when the answer is already clear from the local codebase or from stable language fundamentals.

## Routing priority

1. Project code, symbols, references, and edits → Serena when available.
2. External libraries, frameworks, SDKs, APIs, CLIs, databases, and platforms → Context7 when available.
3. Current vendor or product documentation not covered by Context7 → official documentation or authoritative web sources.
4. Stable, generic programming knowledge → model knowledge is acceptable.
5. If a preferred tool is unavailable → use the next best source and continue; never block the task.
