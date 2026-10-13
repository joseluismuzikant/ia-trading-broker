---
name: strategy-profile
description: Add or tune a simulation type (conservative, inflation protection, aggressive short or medium term, long term) - its bands, limits, filters, prompt block and benchmark. Use when a task mentions strategies, simulation types, allocation bands or risk profiles.
---

# Add or tune a strategy profile

Strategies are declared in code (`app/domain/strategies.py`) and described in
`docs/roadmap.md` section 5. That table and the code must agree.

1. **Profile.** Key, label, goal, horizon, bands (group → subtypes → min/max
   %), max per position, max turnover, max trades, filters, indicators to
   stress, benchmark series, and a short prompt block in plain language.
2. **Groups** are defined over catalog subtypes (`config/instruments.toml`).
   Groups may overlap. A symbol not in the catalog belongs to no band.
3. **Coherence tests** for every profile:
   - the sum of band minimums is at most 100 % and the bands can reach 100 %;
   - the suggested universe contains instruments for every band with a
     minimum above zero;
   - max per position times max trades can actually move the portfolio into
     the bands.
4. **Validation.** Band, turnover and filter checks live in
   `advice_validation.py` and produce plain-language errors that the graph
   sends back to the model on retry. When user limits and strategy limits
   disagree, the stricter wins.
5. **Proposals copy the profile** they used, so tuning a profile never changes
   how an old proposal was judged.
6. **Docs.** Update the table in `docs/roadmap.md` section 5 and the help page.

Values are design starting points, not investment advice: say so in the help
page.
