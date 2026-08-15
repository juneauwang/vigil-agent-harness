# Vigil CLI Reference

Live sources when anything looks stale: `vigil --help`, `vigil <command> --help`,
https://github.com/juneauwang/vigil-agent-harness

### Global Flags

```
vigil [flags] [command]        (no subcommand = interactive chat)

  --version, -V             Show version
  -z, --oneshot PROMPT      One-shot: print ONLY the final response (for scripts/pipes)
  -m MODEL  --provider P    Model/provider override for this invocation
  -t, --toolsets LIST       Comma-separated toolsets for this invocation
  --resume, -r SESSION      Resume session by ID or title
  --continue, -c [NAME]     Resume by name, or most recent session
  --worktree, -w            Isolated git worktree mode (parallel agents)
  --skills, -s SKILL        Preload skills (comma-separate or repeat)
  --profile, -p NAME        Use a named profile
  --yolo                    Skip dangerous command approval
  --tui / --cli             Force the Ink TUI / classic REPL
  --ignore-rules            Skip AGENTS.md/SOUL.md/memory/skill injection
  --safe-mode               Disable ALL customizations (troubleshooting)
  --pass-session-id         Include session ID in system prompt
```

### Chat

```
vigil chat [flags]
  -q, --query TEXT          Single query, non-interactive
  --image PATH              Attach a local image to a single query
  -Q, --quiet               Suppress banner, spinner, tool previews
  --checkpoints             Enable filesystem checkpoints (/rollback)
  --max-turns N             Cap tool-calling iterations
  --source TAG              Session source tag (default: cli)
```
(plus the global flags above)

### Configuration

```
vigil setup [section]      Wizard (model|tts|terminal|gateway|tools|agent)
vigil model                Interactive model/provider picker
vigil fallback [add|remove|list]  Fallback provider chain
vigil config [show|edit|get|set|unset|path|env-path|check|migrate]
vigil login / logout       OAuth sign-in / clear stored auth
vigil doctor [--fix]       Check dependencies and config
vigil status [--all]       Component status
```

### Tools & Skills

```
vigil tools [list|enable NAME|disable NAME]   Per-platform toolsets (curses UI with no args)

vigil skills list|browse|search QUERY|inspect ID
vigil skills install ID    Hub identifier OR a direct https://…/SKILL.md URL
vigil skills config        Enable/disable skills per platform
vigil skills check|update|uninstall|publish PATH
vigil skills tap add REPO  Add a GitHub repo as a skill source
vigil bundles              Skill bundles (one /<name> alias loads several skills)
```

### MCP Servers

```
vigil mcp add NAME (--url or --command) | remove | list | test NAME
vigil mcp catalog | install NAME     Curated catalog install
vigil mcp configure NAME             Toggle tool selection
vigil mcp serve                      Run Vigil as an MCP server
```
Details (transport, tool discovery, catalog): `references/native-mcp.md`.

### Gateway (Messaging Platforms)

```
vigil gateway run|install|start|stop|restart|status|setup
```

20+ platforms: Telegram, Discord, Slack, WhatsApp (Baileys + Business Cloud API), iMessage (Photon — `vigil photon setup`), Signal, Email, SMS, Matrix, Mattermost, Teams, LINE, SimpleX, ntfy, Google Chat, Home Assistant, DingTalk, Feishu, WeCom, Weixin, API Server, Webhooks. Open WebUI connects via the API Server adapter. Most adapters ship under `plugins/platforms/`.
Docs: https://github.com/juneauwang/vigil-agent-harness

### Sessions

```
vigil sessions list|browse|rename ID TITLE|delete ID|export OUT|prune|stats
```

### Cron / Webhooks

```
vigil cron list|create SCHED|edit ID|pause|resume|run ID|remove|status
    Schedules: '30m', 'every 2h', '0 9 * * *', ISO timestamp
vigil webhook subscribe NAME|list|remove NAME|test NAME
```
Webhook payloads/routes: `references/webhooks.md`.

### Profiles

```
vigil profile list|create NAME (--clone|--clone-all|--clone-from)|use|show|delete
vigil profile rename A B | alias NAME | export NAME | import FILE
```

### Credentials & Pools

```
vigil auth                 Interactive credential manager
vigil auth add [PROVIDER]  Add OAuth or API-key credential (nous, openai-codex, qwen-oauth, …)
vigil auth list|remove P IDX|reset PROVIDER|status
```
Multiple credentials per provider form a pool that rotates automatically and skips exhausted keys.

### Other

```
vigil desktop / gui        Native desktop app
vigil dashboard            Web admin panel + embedded chat (--stop / --status)
vigil proxy                OpenAI-compatible local proxy backed by an OAuth provider
vigil portal               Quick setup / sign in via Nous Portal
vigil kanban <verb>        Multi-agent work-queue board
vigil project              Named multi-folder workspaces
vigil skin list|use|set    Switch/tweak skins (see references/themes.md)
vigil pets <verb>          Pet mascots (see references/petdex.md)
vigil memory setup|status|off|reset   Memory provider
vigil secrets bitwarden|onepassword   External secret stores
vigil moa                  Mixture-of-Agents slots
vigil hooks / security / backup / import / checkpoints / console
vigil logs [-f] [errors]   View agent/error logs
vigil send                 One-off message through a gateway platform
vigil pairing / plugins / insights / journey / computer-use
vigil acp                  ACP server (IDE integration)
vigil completion bash|zsh|fish
vigil update / uninstall / claw migrate
```

Plugin- and provider-supplied subcommands (e.g. `vigil photon setup`) only appear once their plugin is installed/active.

### Where to Find Things

| Looking for... | Location |
|---|---|
| Config options | `vigil config edit` · [Configuration docs](https://github.com/juneauwang/vigil-agent-harness) |
| Tools / toolsets | `vigil tools list` · [Tools reference](https://github.com/juneauwang/vigil-agent-harness) |
| Skills catalog | `vigil skills browse` · [Skills catalog](https://github.com/juneauwang/vigil-agent-harness) |
| Provider setup | `vigil model` · [Providers guide](https://github.com/juneauwang/vigil-agent-harness) |
| Env variables | `vigil config env-path` · [Env vars reference](https://github.com/juneauwang/vigil-agent-harness) |
| Gateway logs | `~/.vigil/logs/gateway.log` (or `vigil logs`) |
| Sessions | `vigil sessions browse` (reads state.db) |
