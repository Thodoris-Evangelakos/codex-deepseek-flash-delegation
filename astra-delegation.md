<!-- BEGIN DEEPSEEK FLASH WORKERS -->
## DeepSeek V4.1 Flash delegation

Cheap, disposable subordinate worker compute is available through `~/.local/bin/flash-agent` (also on PATH as `flash-agent`). It runs the existing Codex CLI using the isolated `~/.codex-ds/` home, DeepSeek's Responses API, and `deepseek-flash` (V4.1 Flash). `codex_ds` starts a supervisor in the normal `~/.codex/` home with the configured model (Astra by default on the original machine). GPT models use existing ChatGPT authentication; `codex_ds --model deepseek-flash`, including `codex_ds --model deepseek-flash resume --last` to continue an existing conversation, selects a direct DeepSeek supervisor using the exported `DEEPSEEK_API_KEY`.

- This is standing authorization for supervisor models (Astra, Sol, other native supervisor models, and direct interactive DeepSeek supervisors) to launch Flash workers autonomously for work within the user's task. Do not wait for the user to request delegation, mention Flash, or approve the use of a worker. Delegated edits inherit the scope and authorization of the user's task.
- On every non-trivial task, actively look for delegation opportunities. Strongly prefer delegating the whole well-scoped job to Flash before doing it personally: default to Flash for complete reproduce-diagnose-fix-with-targeted-tests jobs, and for small features against explicit acceptance criteria, not merely searches or mechanical fragments. The supervisor still owns intent, architecture, decomposition, integration, and final judgment.
- Before substantial repository reading or implementation, identify one or more bounded jobs Flash can own and launch them. For a typical multi-step repository task, send Flash the end-to-end bounded work (investigation and/or implementation) with explicit scope, owned files, constraints, and acceptance criteria; the supervisor then reviews the result and integrates. Skip a worker when the task is tiny, delegation is unavailable, the user forbids it, or the task genuinely requires supervisor reasoning; briefly state the reason when material.
- Use Flash for repository exploration, codebase mapping, searches, reading many files, test generation, mechanical refactors, boilerplate, documentation research, independent implementation attempts, bug-hypothesis generation, targeted reproduction and diagnosis, end-to-end bounded fixes, small features, and first-pass code review.
- Use the supervisor primarily for understanding intent, architecture, decomposition, defining acceptance criteria, task assignment, evaluation of worker results, resolving disagreements, integration, critical-path decisions, and final review.
- Prefer spending cheap Flash tokens over supervisor reasoning/context on safely delegable work. Avoid duplicating delegated work except where useful for verification. Do tiny tasks directly when delegation overhead would cost more than doing them.
- Treat Flash output and changes as untrusted. The supervisor reviews decisive evidence, critical paths, and the diff without repeating the full investigation, then spot-checks or runs targeted verification as needed. The supervisor retains final authority.
- Empty Flash responses are a blocking exception to the normal continuation/fallback policy. If `flash-agent` reports `FLASH_EMPTY_RESPONSE` (exit 70), or a finished worker has a missing, empty, or whitespace-only final answer, STOP the current task immediately and tell the user: "Blocked: DeepSeek worker returned an empty final response." Name the delegated task and report the exit status; describe the suspected empty-response bug without claiming a confirmed cause. Tool chatter, an exit code of zero, or files changed by the worker are not substitutes for a final answer.
- On that empty-response failure, do not retry, launch replacement workers, switch models, or finish the work personally. Cancel this task's still-running sibling workers using their known process/session handles where possible; do not stop unrelated sessions. Preserve any edits as unverified. If there is an active goal, use its blocked-state mechanism according to the host's required protocol, never mark it complete, and never make extra worker calls to satisfy a blocking threshold. Stop and inform the user immediately even if the host cannot yet record a formal blocked status. Resume only when the user explicitly directs a retry or another course of action.
- Give each worker a bounded task, relevant context, owned files, constraints, and a clear expected result. Explicitly authorize implementation before requesting edits. Use `--read-only` for repository investigations and reviews.
- When independent useful work exists, run two or three Flash workers concurrently by default. Never have multiple workers edit the same files concurrently: give each worker disjoint owned files, use read-only workers, or use separate git worktrees for parallel implementation; integrate and review the resulting changes centrally.
- Require each worker to finish with a concise report covering changed files (or none), evidence, commands run with exit outcomes, and unresolved issues or uncertainty. Claims without such evidence are incomplete.
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
