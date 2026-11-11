# Instructions for your agent

Give your coding agent the essentials without it visiting this site: paste a section into
your repository's agent instructions, or install the Claude Code skill. Both files are
served as-is so an agent can fetch them directly.

## An `AGENTS.md` section

For `AGENTS.md`, `CLAUDE.md`, `.cursor/rules` or any agent instructions file. Replace the
`<path/...>` placeholders with your repository's paths.

Download: [/agents/AGENTS.md](/agents/AGENTS.md)

```bash
curl -s https://caliper.orlalabs.com/agents/AGENTS.md >> AGENTS.md
```

{{AGENTS.md}}

## A Claude Code skill

The same knowledge as a [skill](https://docs.claude.com/en/docs/claude-code/skills) that
Claude Code loads when a task matches its description (comparing models, gating a PR on
regressions or cost, projecting LLM spend).

Download: [/agents/caliper/SKILL.md](/agents/caliper/SKILL.md)

```bash
mkdir -p .claude/skills/caliper
curl -s https://caliper.orlalabs.com/agents/caliper/SKILL.md -o .claude/skills/caliper/SKILL.md
```

Use `~/.claude/skills/caliper/` instead to install it for every project.

{{SKILL.md}}

## Machine-readable docs

| URL | What |
| --- | --- |
| [/llms.txt](/llms.txt) | Index of every page, with links to their Markdown |
| [/llms-full.txt](/llms-full.txt) | Every page in one Markdown file |
| any page URL + `.md` | That page as Markdown, e.g. [/agents/recipes.md](/agents/recipes.md) |
| [/schemas/](/agents/files/) | JSON Schemas for every file Caliper reads or writes |
