# CLAUDE.md

The project instructions are shared by every AI coding assistant and live in
`AGENTS.md`. Claude Code imports them here:

@AGENTS.md

## Claude Code specifics

- Subagents: `.claude/agents/` (`broker-integrator`, `test-writer`,
  `trading-safety-reviewer`). They are thin wrappers around the shared skills.
- Slash commands: `/implement-phase <n>` and `/review`.
- Permissions and the formatting hook: `.claude/settings.json`.

Add Claude-only notes here. Anything that applies to every tool goes in
`AGENTS.md`.
