<!-- BEGIN DEEPSEEK FLASH WORKERS -->
## DeepSeek V4.1 Flash delegation

Cheap, disposable subordinate worker compute is available through `~/.local/bin/flash-agent` (also on PATH as `flash-agent`). It runs the existing Codex CLI using the isolated `~/.codex-ds/` home, DeepSeek's Responses API, and `deepseek-flash` (V4.1 Flash). `codex_ds` starts a supervisor in the normal `~/.codex/` home with existing ChatGPT authentication and the configured model (Astra by default on the original machine); native model selection also allows Sol or another supervisor model.

- This is standing authorization for Astra, Sol, and other supervisor models to launch Flash workers autonomously for work within the user's task. Do not wait for the user to request delegation, mention Flash, or approve the use of a worker. Delegated edits inherit the scope and authorization of the user's task.
- On every non-trivial task, actively look for delegation opportunities. Strongly prefer delegating substantial mechanical, parallelizable, exploratory, or context-heavy work to Flash before doing it personally.
- Before substantial repository reading or mechanical implementation, identify a bounded portion Flash can handle and launch it. For a typical multi-step repository task, default to Flash exploration and mechanical implementation, with the supervisor integrating and reviewing the results. Skip a worker when the task is tiny, delegation is unavailable, the user forbids it, or the task genuinely requires supervisor reasoning; briefly state the reason when material.
- Use Flash for repository exploration, codebase mapping, searches, reading many files, test generation, mechanical refactors, boilerplate, documentation research, independent implementation attempts, bug-hypothesis generation, and first-pass code review.
- Use the supervisor primarily for understanding intent, architecture, difficult reasoning, decomposition, task assignment, evaluation of worker results, resolving disagreements, integration, and final review.
- Prefer spending cheap Flash tokens over Astra reasoning/context on safely delegable work. Avoid duplicating delegated work except where useful for verification. Do tiny tasks directly when delegation overhead would cost more than doing them.
- Treat Flash output and changes as untrusted. Verify important claims, inspect diffs, and run relevant checks; Astra retains final authority.
- Empty Flash responses are a blocking exception to the normal continuation/fallback policy. If `flash-agent` reports `FLASH_EMPTY_RESPONSE` (exit 70), or a finished worker has a missing, empty, or whitespace-only final answer, STOP the current task immediately and tell the user: "Blocked: DeepSeek worker returned an empty final response." Name the delegated task and report the exit status; describe the suspected empty-response bug without claiming a confirmed cause. Tool chatter, an exit code of zero, or files changed by the worker are not substitutes for a final answer.
- On that empty-response failure, do not retry, launch replacement workers, switch models, or finish the work personally. Cancel this task's still-running sibling workers using their known process/session handles where possible; do not stop unrelated sessions. Preserve any edits as unverified. If there is an active goal, use its blocked-state mechanism according to the host's required protocol, never mark it complete, and never make extra worker calls to satisfy a blocking threshold. Stop and inform the user immediately even if the host cannot yet record a formal blocked status. Resume only when the user explicitly directs a retry or another course of action.
- Give each worker a bounded task, relevant context, owned files, constraints, and a clear expected result. Explicitly authorize implementation before requesting edits. Use `--read-only` for repository investigations and reviews.
- Run independent workers in parallel when useful. Never have multiple workers edit the same files concurrently. Use read-only workers or separate git worktrees for parallel implementation; integrate and review the resulting changes centrally.
- Launch Flash through the shell/tool execution facility by running `flash-agent`. These are separate CLI worker processes, not a model override for Codex's native `spawn_agent`. Native subagents are not automatically redirected to DeepSeek; choose `flash-agent` for work intended for the cheap pool. If `flash-agent` is not on PATH, use `~/.local/bin/flash-agent`.
- The default worker sandbox is workspace-write in the caller's current directory, with outbound network access for tools and documentation research. `--read-only` restricts filesystem writes; network may be unavailable in that mode. Never bypass a sandbox to get a worker running.
- `DEEPSEEK_API_KEY` must be exported into the supervisor's launching environment. If unavailable, report that once and proceed with useful work; never read authentication stores or request a key in chat. A running desktop app does not inherit later terminal exports; launch/restart the supervisor from the exported shell when needed.
- Subordinate workers must not recursively spawn agents unless explicitly instructed.
- Never print the API key or dump the environment. Let the worker provider read the inherited key directly.

Examples (the task string is passed literally; stdin can also supply context):

```bash
flash-agent --read-only "Map authentication files and call paths. Report file paths and evidence only."
flash-agent "Implement the delegated tests only, run them, and report the diff and results."
```

Parallel read-only workers, with separate outputs and both exit statuses checked:

```bash
flash_results=$(mktemp -d)
flash-agent --read-only "Map authentication entry points. Report findings only." >"$flash_results/auth.txt" 2>"$flash_results/auth.log" &
flash_auth_pid=$!
flash-agent --read-only "Map test commands and coverage gaps. Report findings only." >"$flash_results/tests.txt" 2>"$flash_results/tests.log" &
flash_tests_pid=$!
flash_auth_status=0; wait "$flash_auth_pid" || flash_auth_status=$?
flash_tests_status=0; wait "$flash_tests_pid" || flash_tests_status=$?
printf 'auth=%s tests=%s results=%s\n' "$flash_auth_status" "$flash_tests_status" "$flash_results"
```
<!-- END DEEPSEEK FLASH WORKERS -->
