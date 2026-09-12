# Codex supervisors with automatic DeepSeek V4.1 Flash delegation

```text
codex_ds -> normal Codex / Astra, Sol, etc. / ChatGPT Plus (~/.codex)
  automatically delegates suitable work, verifies, integrates
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
codex_ds
codex_ds --yolo
codex_ds --model gpt-5.6-sol
codex_ds resume --last
codex_ds exec "Your task here"
```

`codex_ds` starts your normal supervisor in `~/.codex/`, preserving its configured model and authentication. Astra is the current default on this machine; `--model` or the native model selector can select Sol or another available supervisor. All arguments are forwarded unchanged, and the loaded AppArmor profile is used automatically. `--yolo` disables sandboxing and approvals for the supervisor invocation only; Flash workers still explicitly select their own read-only or workspace-write sandbox.

The supervisor loads the standing delegation policy installed in `~/.codex/AGENTS.md`. Give it ordinary tasks: it is instructed to start Flash workers before substantial exploration or mechanical implementation, without waiting for you to ask for delegation. It keeps intent, architecture, integration, and final verification. Tiny tasks can stay local. Plain `codex` also reads this global policy, as originally requested; its model/configuration/authentication remain unchanged.

Only `flash-agent` selects `~/.codex-ds/` and `deepseek-flash`. Cheap workers are separate CLI processes launched through shell tools. Codex's native `spawn_agent` is not transparently redirected to DeepSeek. Automatic selection is driven by the standing instructions, not a forced rewrite of every subagent call. If credentials or sandboxing prevent delegation, the supervisor is instructed to report that limitation rather than imply it used Flash.

Help and version commands work without a DeepSeek key. Actual Flash delegation requires `DEEPSEEK_API_KEY` to be exported in the launching terminal. Start a new supervisor session after installing the updated policy.

## Run workers

```bash
flash-agent --read-only "Inspect the repository and identify every file involved in authentication. Report findings only."
flash-agent "Implement the requested test suite, run the relevant tests, and report exactly what changed."
flash-agent --output /tmp/flash-result.txt "Perform the delegated implementation and report evidence."
printf '%s\n' 'Read the test configuration and report the commands.' | flash-agent --read-only -
```

The default is workspace-write, approval never, outbound network allowed for shell tools, and the caller's current directory. `--read-only` selects the native read-only sandbox; the launcher grants only a private temporary directory for Codex's final-answer file via `--add-dir`, so the repository remains read-only. Outbound network may be unavailable there. Both modes retain Codex's filesystem restrictions. Use a repository/worktree directory as the working directory, not your whole home. Blocked tools return failures; there is no automatic unrestricted fallback. Native hosted web search is disabled as in DeepSeek's integration guide; documentation research can use shell tools in the default sandbox.

Workers are ephemeral CLI processes, not resident daemons. Output goes to stdout and progress to stderr; `--json` enables JSONL events. Normal shell background jobs form the pool; see [astra-delegation.md](astra-delegation.md) for a parallel read-only example with separate logs and exit-code checks. Parallel implementation needs disjoint owned files or separate worktrees. No additional agent spawning is enabled in workers.

### Empty worker responses block the task

The launcher always captures a fresh final-answer file, even when stdout contains progress or JSON events. A missing, empty, or whitespace-only final answer after an otherwise successful Codex exit returns **exit 70** with **`FLASH_EMPTY_RESPONSE`** on stderr. It does not retry. Other nonzero Codex exit codes are preserved. `--output FILE` is written only after a successful non-empty answer, so an existing report cannot make an empty response pass the check. Temporary answer files are removed.

The standing supervisor policy requires it to stop the current task, notify you of the empty worker response, preserve edits as unverified, and avoid retries, replacement workers, model switching, or doing the work itself. It should cancel this task's other running workers where possible. An active goal must remain incomplete and follow the host's formal blocked-status protocol; the user-facing stop and notification are immediate, even if the host imposes a delay before recording `blocked`. Continue only after your explicit direction. This detects the empty-response symptom; it does not assert which provider or client bug caused it.

## Verify

Inside `codex_ds`, `/status` should show your supervisor model (for example Astra or Sol). Give it an ordinary substantial repository task; the tool transcript should show `flash-agent` being launched without a separate user request. The child startup output identifies `model: deepseek-flash` and `provider: deepseek`. The supervisor and workers deliberately have different models. The tests below verify the launcher routing/policy installation and the worker API connection; they do not claim to prove the model's spontaneous delegation decisions.

To test automatic delegation, start a **new** `codex_ds` session in a small repository and give this ordinary task without mentioning Flash or subagents:

```text
Map this repository's entry points, main modules, and test commands. Inspect at most six representative source files and report paths and evidence. Do not modify anything.
```

Pass: the supervisor launches `flash-agent --read-only` before doing most of the exploration itself, the child identifies `deepseek-flash` / `deepseek`, and the supervisor checks and summarizes the result. Fail: it asks whether to delegate, silently does all substantial exploration itself, or uses only expensive native subagents for the mechanical work. A reported missing key or sandbox error is a setup failure, not evidence that delegation worked. The six-file cap bounds this behavioral test; it still uses model tokens.

```bash
python3 ~/.codex-ds/test_flash_agent.py
python3 ~/.codex-ds/smoke-test.py
```

The first check is offline and spends no API tokens. It simulates successful, empty, whitespace-only, and missing answers; verifies exit 70 despite progress output or a stale report; checks that there is exactly one attempt; and verifies cancellation stops the child. This proves the launcher guard, not the supervisor model's obedience. In the behavioral test above, an actual `FLASH_EMPTY_RESPONSE` must make the supervisor report the task as blocked and stop instead of retrying or taking over.

The second check makes one small live worker run in a temporary trivial directory, requires a shell tool to read a random marker, checks the exact final answer from Codex's final-message file and the CLI's model/provider/sandbox fields, and prints non-secret evidence only. Missing credentials, provider errors, sandbox failures, or mismatched output fail the check. On failure, a bounded final-answer excerpt is included with the API key redacted. Raw API responses and environment values are not persisted. Its timeout terminates the worker process group. A selected-model banner alone does not count as a successful live test.

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

For automatic delegation, start your normal supervisor from the key-exported terminal. The worker launcher enters the loaded profile automatically; normal Codex authentication/configuration is unchanged. An already-running desktop supervisor still needs restarting from an environment containing the exported key. If the profile does not resolve a sandbox error on another host, retain the failed output for administrator diagnosis; do not disable the sandbox.

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

Enter the key using the hidden-input command above, then run the offline and live checks. The installer backs up existing files before changes, adds or updates only its delimited delegation section while preserving all surrounding user instructions, and leaves normal config/authentication untouched. Running it again does not duplicate the section. It refuses malformed or duplicate delegation markers before changing files. No Codex installer, login, key copy, or normal configuration rewrite occurs. After installing, run `codex_ds` and give it ordinary tasks.

For private migration, create an archive from the committed files (never archive the entire Codex home). The checked-in config contains no machine-specific project trust entries, and archive ownership/timestamps are stripped:

```bash
tar --format=ustar --owner=0 --group=0 --numeric-owner --mtime=@0 -czf ~/flash-agent-setup.tar.gz -C ~/.codex-ds config.toml models.json AGENTS.md flash-agent astra-delegation.md install.py test_flash_agent.py smoke-test.py README.md codex-flash-worker.apparmor codex_ds
```

The supplied archives contain only those eleven setup files, with no authentication, runtime logs, API keys, normal Codex settings, or local project trust entries. The manual command above includes your current worker config, so review it before publishing a newly generated archive.
