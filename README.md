# Codex supervisors with automatic DeepSeek V4.1 Flash delegation

```text
codex_ds -> normal Codex / Astra, Sol, etc. / ChatGPT Plus (~/.codex)
codex_ds --model deepseek-flash -> main assistant / DeepSeek V4.1 Flash (~/.codex)
  either supervisor delegates suitable work, verifies, integrates
     |-- flash-agent task A --|
     |-- flash-agent task B --| existing Codex CLI, separate processes
                             ~/.codex-ds, deepseek-flash (V4.1 Flash)
                             https://api.deepseek.com/responses
                             DEEPSEEK_API_KEY from the environment
                             caller's workspace, normal Codex sandbox
```

The current official model name for V4.1 Flash is `deepseek-flash`. The older `deepseek-v4-flash` is a temporary compatibility alias and is not used by this setup. The only catalog entry is the complete official `deepseek-flash` entry, fetched on 2026-09-11, including its multimodal support and one-million-token context. Reasoning defaults to the officially recommended `high`.

Sources: [DeepSeek integration and model metadata](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/), [September 10 model rename](https://api-docs.deepseek.com/updates/), [OpenAI configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference). CLI syntax checked with installed `codex-cli 0.154.0`, `codex --help`, and `codex exec --help`. The vendor installer was not run.

## Set the key securely in Bash

Run this in your terminal. Input is hidden and the key is not placed in command history or a file:

```bash
set +x; IFS= read -r -s -p 'DeepSeek API key: ' DEEPSEEK_API_KEY && export DEEPSEEK_API_KEY; printf '\n'
```

The export lasts for that shell and its future children. Start `codex_ds` from this shell to make the key available to the supervisor's workers. An already-running app or Codex process will not inherit a later export. Do not paste the key in chat, config files, command arguments, or startup files. The normal supervisor must retain the exported key in the environment passed to shell tools so `flash-agent` can receive it; this machine's normal configuration already does. If another device customizes shell-environment filters, check that they do not remove it. Worker shell tools themselves filter secret-named variables; only their provider process needs the key. Run `unset DEEPSEEK_API_KEY` when finished.

## Interactive Codex and native commands

```bash
codex_ds                              # normal Codex on your configured model
codex_ds --model deepseek-flash        # start on DeepSeek V4.1 Flash
codex_ds --model gpt-5.6-sol
codex_ds resume --last                 # native session picker
codex_ds --model deepseek-flash resume --last
codex_ds --yolo
```

`codex_ds` is a thin launcher: it forwards every argument to the installed Codex CLI unchanged, so native TUI behavior such as the `/resume` session picker, `/fork`, and `/model` keeps working. The only decision it makes is whether `--model deepseek-flash` (also `-m`, `--model=`, or `-mdeepseek-flash`) appears on that command line. The DeepSeek route selects the `deepseek` provider, the local model catalog, `high` reasoning, and disabled hosted web search; every other launch keeps your normal Codex configuration.

Continue an existing GPT conversation on Flash by resuming it behind the model flag. The resumed thread keeps its history and continues on the DeepSeek provider:

```bash
codex_ds --model deepseek-flash resume --last
codex_ds --model deepseek-flash resume <session-id>
```

`codex_ds --help` prints these forms, and this machine also has a `dsresume` shell shortcut for them. On another device add `dsresume() { codex_ds --model deepseek-flash resume "$@"; }` to your shell rc.

Both providers use the normal `~/.codex` supervisor instructions, session storage, approval policy, and sandbox settings. GPT models use your existing ChatGPT login; DeepSeek uses the exported `DEEPSEEK_API_KEY` and DeepSeek API billing, independently of ChatGPT usage. Changing the provider mid-conversation means quitting and relaunching with the other model; a DeepSeek session lists only `deepseek-flash` in the `/model` picker. The official API name for [V4.1 Flash](https://www.deepseek.com/en/news/deepseek-v4-1-flash/) is `deepseek-flash`.

Direct noninteractive DeepSeek use also remains available:

```bash
codex_ds exec --model deepseek-flash -- "Your task here"
codex_ds --model=deepseek-flash exec "Your task here"
```

The direct route accepts native `--model`/`-m` spellings and forwards arguments literally; text after `--` cannot change providers. DeepSeek uses its Responses API, compatible reasoning settings, and the existing model catalog. Hosted web search is disabled for DeepSeek. The launcher preserves your approval and sandbox settings; `--yolo` affects the supervisor only, while Flash workers keep their own explicit sandbox.

The standing policy in `~/.codex/AGENTS.md` applies only to supervisors launched through `codex_ds`. It directs those supervisors to delegate before broad repository reading, giving Flash complete bounded reproduce-diagnose-fix-test jobs and small features with explicit acceptance criteria. When independent useful work exists, use two or three workers with disjoint owned files or separate worktrees. Workers report changed files, evidence, commands and exit outcomes, and unresolved issues. The supervisor owns intent, architecture, integration, and final judgment, reviewing critical evidence without repeating the entire investigation. Tiny tasks can stay local.

`flash-agent` runs ephemeral workers in `~/.codex-ds`; Codex's native `spawn_agent` is not redirected to DeepSeek. `codex_ds` exports `CODEX_DS_SUPERVISOR=1` for both GPT and DeepSeek supervisors. Plain `codex`, the app, and editor sessions ignore the delegation policy, and `flash-agent` refuses launches without that marker (exit 2), even if the API key is exported. Workers receive no supervisor marker and cannot spawn further workers. Export `DEEPSEEK_API_KEY` before starting `codex_ds`. Start new sessions after installing launcher or policy updates.

## Run workers

These commands are for shell tools inside a `codex_ds` supervisor session:

```bash
flash-agent --read-only "Inspect the repository and identify every file involved in authentication. Report findings only."
flash-agent "Implement the requested test suite, run the relevant tests, and report exactly what changed."
flash-agent --output /tmp/flash-result.txt "Perform the delegated implementation and report evidence."
printf '%s\n' 'Read the test configuration and report the commands.' | flash-agent --read-only -
```

The default is workspace-write, approval never, outbound network allowed for shell tools, and the caller's current directory. `--read-only` restricts the worker's tools; Codex itself writes the final-answer file outside that tool sandbox. The launcher retains a private result-directory `--add-dir` grant; this does not make the workspace writable in read-only mode. Outbound network may be unavailable in read-only mode. Both modes retain Codex's filesystem restrictions. Use a repository/worktree directory as the working directory, not your whole home. Blocked tools return failures; there is no automatic unrestricted fallback. Native hosted web search is disabled as in DeepSeek's integration guide; documentation research can use shell tools in the default sandbox.

The launching process must be able to write its temporary directory and any `--output` destination directory. A worker cannot widen a read-only filesystem inherited from its parent, even if its own command requests workspace-write. If temporary storage or the report destination is unusable, the launcher reports `FLASH_OUTPUT_ERROR` (exit 73) before starting a worker. Check `TMPDIR`, the destination directory, and the parent session's permitted paths. Launch from an appropriately writable context; do not disable the sandbox. This is a filesystem failure, distinct from an empty model answer. `--output` rejects directories, symlinks, and special files, and replaces a regular report atomically after success so a failed copy preserves the previous report.

Workers are ephemeral CLI processes, not resident daemons. Output goes to stdout and progress to stderr; `--json` enables JSONL events. Normal shell background jobs form the pool; see [astra-delegation.md](astra-delegation.md) for a parallel read-only example with separate logs and exit-code checks. Parallel implementation needs disjoint owned files or separate worktrees. No additional agent spawning is enabled in workers.

### Empty worker responses block the task

The launcher always captures a fresh final-answer file, even when stdout contains progress or JSON events. A missing, empty, or whitespace-only final answer after an otherwise successful Codex exit returns **exit 70** with **`FLASH_EMPTY_RESPONSE`** on stderr. It does not retry. Other nonzero Codex exit codes are preserved. `--output FILE` is written only after a successful non-empty answer, so an existing report cannot make an empty response pass the check. Temporary answer files are removed.

The standing supervisor policy requires it to stop the current task, notify you of the empty worker response, preserve edits as unverified, and avoid retries, replacement workers, model switching, or doing the work itself. It should cancel this task's other running workers where possible. An active goal must remain incomplete and follow the host's formal blocked-status protocol; the user-facing stop and notification are immediate, even if the host imposes a delay before recording `blocked`. Continue only after your explicit direction. This detects the empty-response symptom; it does not assert which provider or client bug caused it.

## Verify

Inside `codex_ds`, `/status` should show your supervisor model (for example Astra or Sol). A launch with `codex_ds --model deepseek-flash` shows that model and the `deepseek` provider instead. Give it an ordinary substantial repository task; the tool transcript should show `flash-agent` being launched without a separate user request. The child startup output identifies `model: deepseek-flash` and `provider: deepseek`. Direct DeepSeek supervisors and workers use the same model in separate processes. The tests below verify launcher routing, policy installation, and the worker API connection; they do not prove the model's spontaneous delegation decisions.

To test automatic delegation, start a **new** `codex_ds` session in a small repository and give this ordinary task without mentioning Flash or subagents:

```text
Map this repository's entry points, main modules, and test commands. Inspect at most six representative source files and report paths and evidence. Do not modify anything.
```

Pass: the supervisor launches `flash-agent --read-only` before doing most of the exploration itself, the child identifies `deepseek-flash` / `deepseek`, and the supervisor checks and summarizes the result. Fail: it asks whether to delegate, silently does all substantial exploration itself, or uses only expensive native subagents for the mechanical work. A reported missing key or sandbox error is a setup failure, not evidence that delegation worked. The six-file cap bounds this behavioral test; it still uses model tokens.

```bash
python3 ~/.codex-ds/test_flash_agent.py
python3 ~/.codex-ds/test_flash_sandbox.py
# Run live checks through a codex_ds supervisor's shell tools:
python3 ~/.codex-ds/smoke-test.py
python3 ~/.codex-ds/smoke-test.py --workspace-write
```

The first two checks are offline and spend no API tokens. `test_flash_agent.py` simulates successful, empty, whitespace-only, and missing answers; verifies exit 70 despite progress output or a stale report; checks that there is exactly one attempt; verifies cancellation stops the child; checks the direct DeepSeek routing (`--model`/`-m` variants and native forms, `resume` and `exec` shapes, the `--` prompt boundary, `~/.codex` home and catalog overrides, environment scrubbing, and the missing-key refusal); and drives `codex_ds` on a real pseudo-terminal to confirm interactive launches reach the installed CLI with literal arguments, the normal home, and the portable install. Together they prove the launcher guards, not the supervisor model's obedience. In the behavioral test above, an actual `FLASH_EMPTY_RESPONSE` must make the supervisor report the task as blocked and stop instead of retrying or taking over.

`test_flash_sandbox.py` uses the real native sandbox without making API calls. It checks workspace writes, read-only write denial, outside-workspace write denial, and the explicit preflight failure when the launcher inherits a read-only filesystem. Its temporary runtime home keeps the check isolated from normal Codex state.

Each `smoke-test.py` invocation makes one small live worker run in a temporary directory. Both modes require a shell tool to read a random marker, check the final answer and CLI model/provider/sandbox fields, and print non-secret evidence only. `--workspace-write` additionally requires an actual `apply_patch` edit and verifies the created file byte-for-byte. Missing credentials, provider errors, sandbox failures, or mismatched output fail the check; empty responses retain exit 70. On failure, a bounded final-answer excerpt is included with the API key redacted. Raw API responses and environment values are not persisted. Its timeout terminates the worker process group. A selected-model banner alone does not count as a successful live test.

On 2026-09-12, live read-only output and workspace edits both passed with Codex 0.154.0. The historical empty-response failure was not reproduced; lack of worker tool write access alone does not explain a missing final response. See [Codex CLI documentation](https://learn.chatgpt.com/docs/developer-commands?surface=cli) for `--sandbox` and `--output-last-message` behavior.

This machine initially failed Codex's native sandbox preflight with user-namespace permission errors. The `codex-flash-worker` AppArmor profile is now loaded and sandbox startup succeeds through it. The launcher automatically enters this profile when available; on hosts without it, the launcher uses the ordinary native Codex sandbox. There is no unrestricted fallback, and deprecated Landlock mode is not enabled.

Recheck this host without spending API tokens:

```bash
CODEX_HOME="$HOME/.codex-ds" aa-exec -p codex-flash-worker -- codex sandbox -P :workspace -- /usr/bin/printf 'FLASH_SANDBOX_OK\n'
```

Until this prints `FLASH_SANDBOX_OK`, the setup is installed but not verified operational. A model-only API response would not prove tool execution. On another Linux device, run this preflight there; the restriction may be specific to this host.

### Optional Ubuntu AppArmor repair (administrator required)

Ubuntu documents an application-specific `userns` allowance for applications that build their own sandboxes: [Ubuntu 24.04 release notes](https://documentation.ubuntu.com/release-notes/24.04/). The included `codex-flash-worker.apparmor` is an opt-in named profile using that allowance. Its syntax was validated with AppArmor 4.0.1, and sandbox startup through the loaded profile now succeeds on this machine.

This profile has no executable attachment and does not change ordinary Codex launches. Only processes deliberately launched through `aa-exec -p codex-flash-worker` receive the user-namespace allowance. Codex's filesystem sandbox remains enabled. The setup installer only copies the profile into `~/.codex-ds`; it never installs or loads system policy.

After an administrator reviews the seven-line profile, these commands install and load it. The first command refuses to overwrite an existing system profile:

```bash
sudo sh -c 'test ! -e /etc/apparmor.d/codex-flash-worker && install -o root -g root -m 0644 "$1" /etc/apparmor.d/codex-flash-worker' sh "$HOME/.codex-ds/codex-flash-worker.apparmor" && sudo apparmor_parser -a /etc/apparmor.d/codex-flash-worker
```

Then verify the native sandbox in the opt-in profile:

```bash
CODEX_HOME="$HOME/.codex-ds" aa-exec -p codex-flash-worker -- codex sandbox -P :workspace -- /usr/bin/printf 'FLASH_SANDBOX_OK\n'
```

If that succeeds, enter the API key using the hidden-input command above and run:

```bash
aa-exec -p codex-flash-worker -- python3 ~/.codex-ds/smoke-test.py
aa-exec -p codex-flash-worker -- flash-agent --read-only "Inspect the repository. Report findings only."
```

For automatic delegation, start `codex_ds` from the key-exported terminal. The worker launcher enters the loaded profile automatically; normal Codex authentication/configuration is unchanged. Ordinary Codex sessions do not enable DeepSeek workers. If the profile does not resolve a sandbox error on another host, retain the failed output for administrator diagnosis; do not disable the sandbox.

To remove this optional system policy, first stop processes launched in the profile, then run `sudo apparmor_parser -R /etc/apparmor.d/codex-flash-worker` and `sudo rm /etc/apparmor.d/codex-flash-worker`.

## Install on another Linux device

Prerequisites: Bash, Python 3, and an existing Codex CLI on PATH compatible with the flags above (tested with 0.154.0). Python 3.11+ is recommended for inspecting TOML, but the installer itself uses only the standard library available in Python 3.8+. Keep your device's existing normal Codex setup.

Copy `flash-agent-setup.tar.gz` to the other device, then:

```bash
mkdir -p ~/flash-agent-setup
tar -xzf flash-agent-setup.tar.gz -C ~/flash-agent-setup
python3 ~/flash-agent-setup/install.py
export PATH="$HOME/.local/bin:$PATH"
```

Enter the key using the hidden-input command above, then run the offline and live checks. The installer copies the launcher, worker, policy, and test files. It backs up existing files before changes, adds or updates only its delimited delegation section while preserving all surrounding user instructions, and leaves normal config/authentication untouched. Running it again does not duplicate the section. It refuses malformed or duplicate delegation markers before changing files, and it checks for every bundle file before writing anything. No Codex installer, login, key copy, pip install, or normal configuration rewrite occurs, and authentication stores are never inspected. After installing, run `codex_ds` and give it ordinary tasks.

For private migration, create an archive from the committed files (never archive the entire Codex home). The checked-in config contains no machine-specific project trust entries, and archive ownership/timestamps are stripped:

```bash
tar --format=ustar --owner=0 --group=0 --numeric-owner --mtime=@0 -czf ~/flash-agent-setup.tar.gz -C ~/.codex-ds config.toml models.json AGENTS.md flash-agent astra-delegation.md install.py test_flash_agent.py test_flash_sandbox.py smoke-test.py README.md codex-flash-worker.apparmor codex_ds
```

Include only those setup files in a migration archive, with no authentication, runtime logs, API keys, normal Codex settings, or local project trust entries. The manual command above includes your current worker config, so review it before publishing a newly generated archive.
