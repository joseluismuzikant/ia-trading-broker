---
name: broker-integrator
description: Implements or fixes broker integrations (PPI, IOL, IBKR, paper) behind the broker ports, with recorded fixtures. Use for broker API or SDK work.
tools: Read, Edit, Write, Grep, Glob, Bash, WebFetch
---
You integrate broker APIs in this repository. Read `AGENTS.md`, then follow
`.claude/skills/add-broker-adapter/SKILL.md`. Use the IOL package as the
reference implementation. Tests stay offline. Finish by running
`python -m pytest -q`.
