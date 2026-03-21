# OpenCode Architecture Analysis for autodev Integration

**Date**: 2026-03-21
**Source**: https://github.com/opencode-ai/opencode (archived Sep 2025, Go)
**Purpose**: Evaluate OpenCode as an alternative worker backend for autodev

---

## 1. Tool Execution Model

### OpenCode Architecture

**Key files**: `internal/llm/tools/tools.go`, `internal/llm/agent/agent.go`, `internal/llm/agent/tools.go`

**Tool Interface** (`BaseTool`):
```go
type BaseTool interface {
    Info() ToolInfo
    Run(ctx context.Context, params ToolCall) (ToolResponse, error)
}

type ToolInfo struct {
    Name        string
    Description string
    Parameters  map[string]any
    Required    []string
}

type ToolCall struct {
    ID    string `json:"id"`
    Name  string `json:"name"`
    Input string `json:"input"`  // Raw JSON string
}

type ToolResponse struct {
    Type     toolResponseType `json:"type"`     // "text" | "image"
    Content  string           `json:"content"`
    Metadata string           `json:"metadata,omitempty"`
    IsError  bool             `json:"is_error"`
}
```

**Built-in tools** (13 total):
- File ops: `edit`, `write`, `view`, `patch`, `glob`, `grep`, `ls`
- Execution: `bash` (shell commands via `os/exec.Command`)
- Network: `fetch` (HTTP requests)
- Search: `sourcegraph` (code search)
- Diagnostics: `diagnostics` (LSP-powered)
- Delegation: `agent` (spawns sub-agent with read-only tools)
- External: MCP tools (via `mcp-tools.go`, stdio/SSE transport)

**Tool dispatch loop** (in `agent.go:processGeneration`):
```
for {
    // 1. Stream LLM response, collect tool_calls
    assistantMsg, toolResults, err := streamAndHandleEvents(ctx, sessionID, msgHistory)

    // 2. If LLM requested tools, execute them sequentially
    if finishReason == ToolUse && toolResults != nil {
        // Tool results appended to conversation history
        msgHistory = append(msgHistory, assistantMsg, *toolResults)
        continue  // Loop back for another LLM call
    }

    // 3. Otherwise done
    return AgentEvent{Type: Response, Message: assistantMsg}
}
```

**Key characteristics**:
- Tools are executed **sequentially** within a single turn (no parallel tool execution)
- Each tool receives `context.Context` with `session_id` and `message_id`
- Permission system gates tool execution via pubsub `PermissionRequest` events
- Non-interactive mode auto-approves all permissions (`Permissions.AutoApproveSession(sess.ID)`)
- Tool results are persisted to SQLite as message parts (role=`tool`)
- Sub-agent tool creates child sessions with limited read-only tools (`TaskAgentTools`)

### Comparison with autodev's worker.py

| Aspect | OpenCode | autodev |
|--------|----------|---------|
| **Tool definition** | Go interface (`BaseTool`) with JSON schema params | No direct tool definitions; workers are Claude Code subprocesses that use Claude's built-in tools |
| **Tool dispatch** | In-process Go loop iterating LLM streaming events | Delegated entirely to Claude Code subprocess (autodev doesn't control tool dispatch) |
| **Result capture** | Structured `ToolResponse` persisted to SQLite | `AD_RESULT` JSON marker parsed from stdout |
| **Parallelism** | Sequential tool execution within agent | Multiple workers run in parallel (different subprocesses) |
| **Permission model** | pubsub-based permission service with auto-approve | `--permission-mode auto` flag on Claude subprocess |
| **Sub-agents** | `agent` tool spawns child session with limited tools | Not applicable (autodev uses separate worker processes) |

### Integration gaps:
- **No stdout marker protocol**: OpenCode captures tool results via in-process Go interfaces, not stdout parsing. autodev would need to capture the returned JSON output rather than parsing AD_RESULT markers.
- **Tool set is fixed at compile time**: OpenCode's tools are compiled into the binary. autodev's workers rely on Claude Code's MCP-extensible tool system. OpenCode does support MCP tools, but via its own MCP client.
- **No handoff/discovery protocol**: OpenCode tools don't emit `AD_RESULT`, `commits`, `files_changed`, or `discoveries`. The result is a single text string from the final LLM response.

---

## 2. Context Management

### OpenCode Architecture

**Key files**: `internal/llm/prompt/prompt.go`, `internal/llm/prompt/coder.go`, `internal/config/config.go`

**Project context loading** (`getContextFromPaths()`):
```go
// Default context paths (loaded once via sync.Once):
var defaultContextPaths = []string{
    ".github/copilot-instructions.md",
    ".cursorrules",
    ".cursor/rules/",
    "CLAUDE.md", "CLAUDE.local.md",
    "opencode.md", "opencode.local.md",
    "OpenCode.md", "OpenCode.local.md",
    "OPENCODE.md", "OPENCODE.local.md",
}
```

**System prompt construction** (`GetAgentPrompt()`):
1. Base prompt selected by agent type + provider (separate prompts for Anthropic vs OpenAI)
2. Environment info appended (OS, shell, CWD, date)
3. LSP information appended (available language servers)
4. Project-specific context files concatenated into `# Project-Specific Context` section

**Conversation history strategy**:
- Full message history loaded from SQLite: `messages.List(ctx, sessionID)`
- If session has `SummaryMessageID`, history is truncated from that point (summary replaces older messages)
- Summary message's role is overwritten to `User` to serve as context primer

**Auto-compact / Summarization** (`agent.Summarize()`):
- Triggered when session approaches context limit (95% capacity, checked externally)
- Creates a new session for summarization, sends all messages to `AgentSummarizer`
- Summary stored as a new message; `session.SummaryMessageID` updated
- Old messages before summary point remain in DB but aren't sent to LLM
- Summarizer prompt focuses on: what was done, current work, files being modified, what's next

**Token tracking**:
- `Session` struct tracks `PromptTokens`, `CompletionTokens`, `Cost`
- Updated after each LLM call via `TrackUsage()` with per-model cost rates

### Comparison with autodev's swarm/context.py

| Aspect | OpenCode | autodev |
|--------|----------|---------|
| **Context source** | Config file paths (CLAUDE.md etc) + conversation history | Git state, test output, swarm state, team inbox messages, learnings file, stagnation signals |
| **Context scope** | Single session/conversation | Cross-agent swarm state (all agents, tasks, inbox messages) |
| **History management** | SQLite messages, auto-summarize at 95% capacity | Fixed-size MISSION_STATE.md, planner prompt rebuilt each cycle |
| **Context window strategy** | Summarize and truncate history | Each planner cycle builds fresh context from current state |
| **Project instructions** | Read-once `ContextPaths` (CLAUDE.md etc) | CLAUDE.md injected into worker prompts via Claude Code's own mechanism |
| **Cross-agent context** | None (single agent) | Team inbox messages, shared learnings, swarm state JSON |

### Integration gaps:
- **No cross-agent context**: OpenCode is fundamentally single-agent. It has no concept of team inboxes, shared learnings, or swarm state. autodev's context system aggregates data from multiple agents.
- **Summarization is promising**: OpenCode's auto-compact via `AgentSummarizer` could be useful for long-running autodev workers that exceed context limits. autodev currently doesn't have per-worker context management.
- **Static vs dynamic context**: OpenCode loads project context once (`sync.Once`); autodev rebuilds context each planner cycle from live state.

---

## 3. Session Lifecycle

### OpenCode Architecture

**Key files**: `internal/session/session.go`, `internal/message/message.go`, `internal/db/`

**Session struct**:
```go
type Session struct {
    ID               string  // UUID
    ParentSessionID  string  // For sub-agent/title sessions
    Title            string
    MessageCount     int64
    PromptTokens     int64
    CompletionTokens int64
    SummaryMessageID string  // Points to summary when compacted
    Cost             float64
    CreatedAt        int64   // Unix timestamp
    UpdatedAt        int64
}
```

**Session Service interface**:
```go
type Service interface {
    pubsub.Suscriber[Session]
    Create(ctx context.Context, title string) (Session, error)
    CreateTitleSession(ctx context.Context, parentSessionID string) (Session, error)
    CreateTaskSession(ctx context.Context, toolCallID, parentSessionID, title string) (Session, error)
    Get(ctx context.Context, id string) (Session, error)
    List(ctx context.Context) ([]Session, error)
    Save(ctx context.Context, session Session) (Session, error)
    Delete(ctx context.Context, id string) error
}
```

**Session types**:
1. **Main session**: Created via `Create(ctx, title)`, gets UUID
2. **Task session**: Child of main, via `CreateTaskSession()` -- used by sub-agent tool
3. **Title session**: Via `CreateTitleSession()` -- for async title generation

**Lifecycle**:
```
Create -> Store in SQLite -> Load messages on resume ->
Execute with full history -> Auto-compact if needed ->
Persist results -> Track cost -> Cleanup on shutdown
```

**Persistence**:
- All state in SQLite via sqlc-generated queries
- Messages stored as JSON-serialized `Parts` (text, tool_call, tool_result, reasoning, binary, finish)
- Sessions support `--continue` / `--session <id>` CLI flags for resumption
- `--fork` flag creates a new session branching from an existing one

**Message content model**:
```go
type Message struct {
    ID        string
    SessionID string
    Role      MessageRole  // "user" | "assistant" | "tool"
    Parts     []ContentPart  // polymorphic: TextContent, ToolCall, ToolResult, ReasoningContent, etc.
    Model     ModelID
    CreatedAt int64
    UpdatedAt int64
}
```

**Content part types**: `text`, `reasoning`, `image_url`, `binary`, `tool_call`, `tool_result`, `finish`

### Comparison with autodev's session.py

| Aspect | OpenCode | autodev |
|--------|----------|---------|
| **Session concept** | Persistent conversation with messages, SQLite-backed | Ephemeral subprocess; no persistent session concept |
| **Resumption** | `--continue` / `--session <id>` loads full history | No session resumption; each worker starts fresh |
| **State persistence** | Full message history + tool results in SQLite | `AD_RESULT` parsed from stdout, handoff data in autodev's SQLite |
| **Parent/child** | `ParentSessionID` for sub-agent delegation | No parent/child worker relationship |
| **Cost tracking** | Per-session token counts + cost | Not tracked per-worker (aggregated at mission level) |
| **Output extraction** | `AgentEvent.Message.Content()` returns structured response | `AD_RESULT:{}` marker parsed from raw stdout |

### Integration gaps:
- **Session != Worker**: OpenCode sessions are conversations; autodev workers are task executors. An OpenCode session could persist across multiple autodev task dispatches (session resumption), but autodev's current model is fire-and-forget.
- **Resumption opportunity**: OpenCode's `--continue` flag could enable a "resume on failure" pattern for autodev workers, rather than restarting from scratch. This is a significant capability gap in autodev today.
- **Structured output vs marker parsing**: OpenCode returns structured `AgentEvent` with typed fields. autodev parses `AD_RESULT` from stdout. Integration would need a translation layer.

---

## 4. Process Model

### OpenCode Architecture

**Key files**: `cmd/root.go`, `internal/app/app.go`

**Execution modes**:

1. **Interactive TUI** (default): `opencode` or `opencode tui`
   - Bubble Tea program with full event loop
   - pubsub routes agent/permission/message events to TUI

2. **Non-interactive** (`opencode run --command "..."` or legacy `opencode -p "..."`):
   - Creates session, auto-approves all permissions
   - Blocks on `agent.Run()` -> waits for `AgentEvent` on channel
   - Outputs formatted result (text or JSON) to stdout
   - Returns exit code 0 on success

3. **Server mode** (`opencode serve`):
   - Headless API server without TUI on configurable port
   - Supports mDNS discovery and CORS
   - Attachable via `opencode attach`

4. **Web mode** (`opencode web`):
   - Server + browser-based UI

**CLI flags for headless/programmatic use**:
```
opencode run --command "prompt text"    # Non-interactive execution
             --format json              # JSON output format
             --model provider/model     # Model override
             --agent coder|task         # Agent type
             --session <id>             # Resume session
             --continue                 # Continue last session
             --fork                     # Fork when resuming
             --file <path>              # Attach files
             --title "session title"    # Custom title

opencode serve --port 8080             # Headless API server
               --hostname 0.0.0.0
               --mdns                   # mDNS discovery

# Global flags:
--log-level DEBUG|INFO|WARN|ERROR
--print-logs                            # Print logs to stderr
```

**Non-interactive flow** (`app.RunNonInteractive()`):
```go
func (a *App) RunNonInteractive(ctx, prompt, outputFormat, quiet) error {
    sess := a.Sessions.Create(ctx, title)
    a.Permissions.AutoApproveSession(sess.ID)  // Auto-approve all tools
    done := a.CoderAgent.Run(ctx, sess.ID, prompt)
    result := <-done  // Block until agent completes
    content := result.Message.Content().String()
    fmt.Println(format.FormatOutput(content, outputFormat))
}
```

**Bash tool subprocess model**:
- `os/exec.Command` with persistent shell session via `internal/llm/tools/shell/`
- Configurable shell via `ShellConfig{Path, Args}`
- Default timeout: 60s, max: 600s
- Output truncation at 30KB
- Banned commands list (curl, wget, nc, etc.)
- Safe read-only allowlist for auto-approval

**App orchestrator**:
```go
type App struct {
    Sessions    session.Service
    Messages    message.Service
    History     history.Service
    Permissions permission.Service
    CoderAgent  agent.Service
    LSPClients  map[string]*lsp.Client
}
```

### Comparison with autodev's backends/local.py

| Aspect | OpenCode | autodev |
|--------|----------|---------|
| **Invocation** | Go binary: `opencode run --command "..."` | Python: `asyncio.create_subprocess_exec("claude", ...)` |
| **Output capture** | Structured `AgentEvent` (in-process) or text/JSON stdout | Raw stdout parsed for `AD_RESULT:{}` marker |
| **Process model** | Single process, in-process tool execution | Subprocess per worker, Claude handles tools internally |
| **Workspace isolation** | Single working directory (`--cwd` flag) | `WorkspacePool` with `git clone --shared` per worker |
| **Concurrency** | One agent per `App` instance (session busy lock) | Multiple workers via separate subprocesses |
| **Health checking** | LSP client shutdown with 5s timeout | `_health_check_workspace()` verifies .venv, git status |
| **Output format** | `--format json` gives structured JSON | `output-format text` parsed for AD_RESULT markers |
| **Session management** | Built-in SQLite sessions with resume | No session persistence; fresh subprocess each time |
| **MCP support** | Native MCP client (stdio/SSE) | `--mcp-config` passed to Claude subprocess |

### Integration paths for autodev:

**Option A: Subprocess backend** (recommended)
```python
cmd = ["opencode", "run", "--command", prompt, "--format", "json", "--cwd", workspace_dir]
proc = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
stdout, _ = await proc.communicate()
result = json.loads(stdout)
```
- Pro: Drop-in replacement, JSON output eliminates fragile AD_RESULT parsing
- Con: No AD_RESULT protocol (need adapter layer), Go binary dependency

**Option B: OpenCode serve mode** (HTTP API backend)
```python
proc = await asyncio.create_subprocess_exec("opencode", "serve", "--port", str(port))
response = await http_client.post(f"http://localhost:{port}/api/run", json={"prompt": prompt})
```
- Pro: Clean API boundary, multiple sessions, structured responses
- Con: HTTP overhead, undocumented API, server lifecycle management

**Option C: Go library** (not feasible without CGo/gRPC wrapper)

---

## 5. Summary: Key Findings

### Strengths for autodev integration

1. **Non-interactive mode is production-ready**: `opencode run --command "..." --format json` provides clean headless execution with structured JSON output. More robust than parsing AD_RESULT markers.

2. **Session resumption**: `--continue` and `--session <id>` enable resuming failed workers rather than starting fresh. Significant capability gap in autodev today.

3. **Auto-compact/summarization**: Built-in context management prevents context window overflow during long-running tasks.

4. **Multi-provider support**: Anthropic, OpenAI, Gemini, Bedrock, Copilot, Groq, Azure, VertexAI. Enables cost optimization by routing simple tasks to cheaper models.

5. **Server mode**: `opencode serve` provides headless HTTP API as clean integration point.

6. **Built-in cost tracking**: Per-session token/cost accounting missing from autodev workers.

7. **MCP support**: Native MCP client (stdio/SSE), matching autodev's approach.

### Gaps and incompatibilities

1. **No AD_RESULT protocol**: Returns text/JSON from LLM, not `{status, commits, files_changed, discoveries, concerns}`. Adapter required.

2. **No workspace isolation**: Uses CWD, no WorkspacePool equivalent. Must use autodev's existing WorkspacePool.

3. **Single-agent concurrency**: `IsSessionBusy()` lock, one agent per App. Parallel workers need separate processes.

4. **Go binary dependency**: Operational complexity increase for Python project.

5. **Archived repository**: Sep 2025, continues as "Crush". Long-term maintenance uncertain.

6. **Sequential tool execution**: No parallel tool calls within a session.

7. **No file-lock coordination**: No equivalent to autodev's `file_lock_registry.py`.

8. **Fixed tool set**: Compiled in. Cannot dynamically add autodev-specific tools without forking.

### Recommended integration approach

Create `backends/opencode.py` implementing `WorkerBackend`:
1. Invoke `opencode run --command <prompt> --format json --cwd <workspace_dir>`
2. Parse JSON stdout into autodev's `MCResultSchema` via adapter
3. Use autodev's existing `WorkspacePool` for workspace isolation
4. Leverage `--session` for retry-with-context on failure
5. Add `OpenCodeConfig` to config.py with model/agent/provider settings
6. Planner selects backend per-task based on model requirements
