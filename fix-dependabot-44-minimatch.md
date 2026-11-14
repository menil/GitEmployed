# AI Decision Record: Override minimatch to resolve GHSA-g3hv-75wg-m249

## Context & Goal
- **Alert**: Dependabot Alert #44 (GHSA-g3hv-75wg-m249 / CVE-2026-118764, Moderate severity, CVSS 6.9).
- **Target Package**: `minimatch` in `installer/package-lock.json`.
- **Vulnerability**: Pattern amplification denial-of-service in `minimatch` when `optimizationLevel >= 2` for versions `>= 7.0.0, < 11.0.1`.
- **Goal**: Upgrade `minimatch` to a patched version (`>= 11.0.1`) while preserving compatibility across the installer test and build suites.

## Architecture & Key Decisions
- `minimatch` is a transitive development dependency pulled in by `eslint` (`^10.2.6`), `@typescript-eslint/typescript-estree` (`^10.2.2`), and `@eslint/config-array` (`^10.2.4`).
- Because upstream tools currently restrict their ranges to `^10.x.x`, npm will not automatically bump `minimatch` to `11.x` via standard semver resolution.
- We added `"minimatch": "^11.0.1"` to `overrides` in `installer/package.json`, which mirrors our existing practice (e.g. `esbuild: "^0.28.1"`).
- In npm, `overrides` forces resolution to `11.0.8` across the entire `installer` dependency tree without breaking eslint or vitest.

## Alternatives Considered & Rejected
- **`npm audit fix --force`**: Rejected because npm proposes downgrading `@typescript-eslint/parser` to `6.15.0`, introducing unnecessary breaking changes and regressions across modern TypeScript AST tooling.
- **Waiting for upstream updates**: Rejected because Dependabot alerts remain open and fail security scans until upstream releases propagate; the `overrides` field cleanly fixes the vulnerability immediately with zero runtime risk.
