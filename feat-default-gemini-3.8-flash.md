# AI Decision Record: Update Default Gemini Model to Gemini 3.8 Flash

## Context & Goal
GitEmployed uses LLMs to evaluate incoming job postings against a candidate's resume across five dimensions (tech stack, experience, location, salary, industry domain) and tailors resume variants for approved matches. Previously, the default Gemini model was pinned to `models/gemini-2.5-flash`.

In real-world triage usage, Gemini 2.5 Flash exhibited pronounced grade inflation and lenient scoring: it rarely assigned "B" fit grades or closed mismatched roles, in contrast to frontier models like Claude Sonnet which evaluate prerequisites more strictly. The objective was to update the default model to `gemini-3.8-flash` to improve triage discernment and reasoning quality while keeping latency and costs minimal.

## Architecture & Key Decisions
1. **Model Default Migration**:
   - Updated `_DEFAULT_GEMINI_MODEL` and `GeminiClient` in `src/gitemployed/llm.py` to `models/gemini-3.8-flash`.
   - Updated user documentation in `DEVELOPMENT.md` and `template/README.md`.
   - Updated unit tests in `tests/test_llm.py` and settings fixture comments in `tests/fixtures/settings.yaml`.
2. **Environment & Testing Resilience**:
   - Added `pkgs.nodejs` to `devenv.nix` packages to ensure that local development and git hooks run against a modern Node environment (Node 20.12+ required by `vitest` / `rolldown` in `installer/package.json`), avoiding dependency on unpinned host Node versions.
3. **Backwards Compatibility**:
   - The flexible prefix parsing and normalization in `LiteLLMClient._resolve_litellm_model` remains intact. Users who prefer custom models (e.g. `gemini-2.5-pro` or legacy `gemini-2.5-flash`) can still configure `GEMINI_MODEL` freely.

## Alternatives Considered & Rejected
1. **Defaulting to `gemini-2.5-pro`**:
   - *Consideration*: Gemini 2.5 Pro offers deep reasoning comparable to Claude Sonnet and would eliminate grade inflation entirely.
   - *Rejection*: Pro tier costs significantly more and has higher latency. Flash models remain the ideal default for automated background scrapers (e.g. daily runs of 50-100 roles), with Gemini 3.8 Flash providing improved reasoning without the Pro cost and latency penalty.
2. **Adjusting Global Fit Thresholds Instead**:
   - *Consideration*: Raising the default `fit_threshold` from `3.5` to `4.0` or `4.2` to counter Flash grade inflation without touching the model.
   - *Rejection*: Adjusting the threshold globally would break calibration for users running stricter models like Claude Sonnet (where 3.5 is already hard to achieve), causing excessive false-negative mismatch closures.
