# AI Decision Record: Strip Gemini `models/` Prefix & Enforce Provider Precedence

## Context & Goal

Switching back to `GEMINI_API_KEY` caused `Triage and Tailor Issue` runs to fail with HTTP `404 Not Found` (`litellm.NotFoundError: GeminiException`).

- `_DEFAULT_GEMINI_MODEL` (`"models/gemini-2.5-flash"`) retained the `models/` prefix required by the previously removed `google-generativeai` SDK.
- `LiteLLMClient._resolve_litellm_model` prepended `"gemini/"` without stripping `"models/"`, passing `"gemini/models/gemini-2.5-flash"` to LiteLLM.
- LiteLLM stripped `"gemini/"` and interpolated `"models/gemini-2.5-flash"` after `/v1beta/models/`, requesting `https://generativelanguage.googleapis.com/v1beta/models/models/gemini-2.5-flash:generateContent` (`404`).
- Additionally, self-review identified that evaluating `model_name.startswith(("models/", "gemini-", "gemini/"))` ahead of explicit `provider` checks could misroute OpenRouter API keys or Claude `sk-ant-oat` OAuth tokens to Google's endpoint if a Gemini `research.model` override remained in `settings.yaml`.

## Architecture & Key Decisions

1. **Normalize `litellm_model` while preserving `model_name` backwards compatibility**:
   - `_resolve_litellm_model` strips leading `"gemini/"` and `"models/"` segments before prepending `"gemini/"`, normalizing `"models/gemini-2.5-flash"`, `"gemini-2.5-flash"`, and `"gemini/gemini-2.5-flash"` to `"gemini/gemini-2.5-flash"`.
   - Keeping `client.model_name` as supplied preserves compatibility with existing `GEMINI_MODEL` repository variables, `config/settings.yaml` overrides, and schema tests.
2. **Authoritative explicit-provider dispatch**:
   - `_resolve_litellm_model` evaluates explicit `provider` values (`gemini`, `openrouter`, `claude`/`anthropic`) before falling back to `model_name` prefix inference when `provider` is `None`.
   - `_call_litellm` restricts `sk-ant-oat` `Authorization` and `anthropic-beta` header injection to `self.litellm_model.startswith("anthropic/")`.
3. **Consistent `get_llm_client` boundary validation**:
   - `get_llm_client` validates the stripped Gemini model slug (`gemini-<model>` with no embedded `/`), accepting `gemini-*`, `models/gemini-*`, and `gemini/gemini-*` while failing fast on bare/degenerate prefixes (`"models/"`, `"gemini/"`, `"models/gpt-4o"`).
   - Claude validation similarly enforces `claude-*` or `anthropic/claude-*` rather than accepting arbitrary `vendor/model` strings.
4. **Transport-level regression test**:
   - Existing unit tests mocked `litellm.completion`, hiding LiteLLM's URL interpolation bug. Added `test_gemini_client_builds_unprefixed_wire_url` patching `HTTPHandler.post` so real `litellm.completion` constructs and asserts the wire URL `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent`.

## Alternatives Considered & Rejected

- **Changing `_DEFAULT_GEMINI_MODEL` to `"gemini-2.5-flash"` and dropping `models/` support**: Rejected because existing user forks, documentation, and `GEMINI_MODEL` repository variables already specify `models/gemini-*`; breaking those configurations is unnecessary when stripping `models/` at the LiteLLM boundary handles both transparently.
- **Keeping interleaved `provider` and `model_name.startswith(...)` checks**: Rejected because adding `model_name.startswith("gemini/")` widened cross-provider hijacking whenever `OPENROUTER_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` was paired with a leftover Gemini `research.model` override.
