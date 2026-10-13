---
name: implement-phase
description: Work through one phase of docs/roadmap.md item by item, with tests, README updates and a final check. Use when asked to implement a roadmap phase or a roadmap checklist item.
---

# Implement a roadmap phase

1. Read `AGENTS.md`, then the phase in `docs/roadmap.md`, its acceptance
   check, and section 1 (invariants). Read every file the phase names before
   editing.
2. Make a short plan: one entry per checklist item, in order, with the files
   each one touches and the tests that prove it. If an item depends on an
   open decision in section 2 of the roadmap, stop and ask.
3. For each item:
   - write or update the tests first when the behavior is clear;
   - make the smallest change that completes the item;
   - run `python -m pytest -q` (and `ruff check . && ruff format --check .`
     once ruff exists); fix failures before moving on;
   - tick the item in `docs/roadmap.md`.
4. Schema changes: tables are not migrated. Write the `ALTER TABLE`
   statements for every new or changed column and include them in the
   summary for the PR description.
5. Keep `README.md` accurate in the same change: configuration, endpoints,
   the Status table (remove a known issue once it is fixed), smoke tests.
6. If the phase touches orders, approvals, execution, credentials, auth or
   events, run the `trading-safety-review` skill on the diff before finishing.
7. Finish with: what was done, the acceptance check result, the
   `ALTER TABLE` statements, and anything left open.

Never weaken a test, skip one, or relax an invariant to finish a phase.
