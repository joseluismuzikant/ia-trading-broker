---
name: add-llm-provider
description: Add a model provider to config/models.toml or fix how a provider is called (Responses API, Chat Completions, Anthropic). Use when a task mentions a model, provider, API key, structured output, or the model catalog.
---

# Add or fix a model provider

The model catalog is `config/models.toml`; the advisor seam is
`app/services/advice.py::build_advisor`; the port is
`app/ports/portfolio_advisor.py`.

1. **Pick the API style** and set `api_type` on the catalog entry:
   - `responses` — OpenAI Responses API with strict JSON schema (OpenAI,
     providers that implement it);
   - `chat_completions` — OpenAI-compatible Chat Completions with JSON mode
     or tool calling, parsed and validated with Pydantic (DeepSeek, Gemini's
     OpenAI-compatible endpoint, Xiaomi MiMo);
   - `anthropic` — native Anthropic SDK with tool use for structured output
     (Claude).
   Confirm the style against the provider's current documentation; do not
   assume.
2. **One advisor per API style**, not per provider. A new OpenAI-compatible
   provider is a catalog entry only.
3. **Contract.** Every advisor returns a `PortfolioAdvice` validated by
   Pydantic. Provenance (provider, model) is stamped from the catalog entry,
   never read from the answer. One call per run; the retry on validation
   errors is the graph's job, not the advisor's.
4. **Limits.** Per-entry timeout (reasoning models are slow), and the same
   temperature for every provider so comparisons are fair.
5. **Keys.** Read from the environment variable named by `api_key_env` (or the
   per-user encrypted store once roadmap phase 3 lands). Never in TOML, logs,
   traces or events.
6. **Tests.** A fake client per API style that returns recorded answers,
   including a malformed one, an empty one and a timeout. No network.
7. **Docs.** Update `.env.example`, the README Configuration section and the
   model list.
