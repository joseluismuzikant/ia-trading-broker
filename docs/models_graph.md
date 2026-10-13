# Advisor models

The portfolio-advice port has two implementations: one that reaches a model
endpoint, and the offline simulator a catalog entry with `kind = "fake"` builds.
Nothing else may answer for a model.

```text
PortfolioAdvisor
├── OpenAIPortfolioAdvisor
└── FakePortfolioAdvisor   # offline simulator (catalog kind = "fake")
```

## What each one is

- **`PortfolioAdvisor`** — `app/ports/portfolio_advisor.py`. The provider-
  independent contract: one call, one whole `PortfolioAnalysisContext` in,
  one `PortfolioAdvice` out. It never returns quantities or broker requests.

- **`OpenAIPortfolioAdvisor`** — `app/infrastructure/openai/portfolio_advisor.py`.
  The only implementation that reaches the network. It asks one configured
  model for a whole-portfolio target allocation, in one call. Which model
  answers is configuration: an entry from [`config/models.toml`](../config/models.toml)
  supplies the model id, the endpoint, and the environment variable holding
  that provider's key. Provenance is stamped from that entry, never read back
  from the model.

- **`FakePortfolioAdvisor`** — `app/infrastructure/simulation/portfolio_advisor.py`.
  The offline simulator. It never reaches a network and returns a valid
  self-financing allocation by default (the account kept as it is). A catalog
  entry with `kind = "fake"` selects it through the one seam in
  `app/services/advice.py::build_advisor`, so a smoke test can run the whole
  flow with no provider key. The test suite uses the same class and patches
  the seam automatically.

## Rules

- The simulator answers only for catalog entries declared `kind = "fake"`;
  every other entry builds the OpenAI advisor. Tests patch the seam unless a
  test explicitly opts out (`real_advisors`).
- There is no rule-based fallback advisor: if a model cannot answer, the run
  stops and no proposal is saved.
- The model advises. Deterministic Python validates the allocation, sizes the
  orders, and checks the risk limits.
