#!/usr/bin/env python3
"""Offline regression check: argument safety, isolation, failures, and backup preservation."""
import json
import os
import pty
import shutil
import subprocess
import tempfile
import time
import signal
import importlib.util
from pathlib import Path

root = Path(__file__).resolve().parent
with tempfile.TemporaryDirectory(prefix="flash-check-") as directory:
    temp = Path(directory)
    stub = temp / "codex"
    stub.write_text('''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
if sys.argv[1:] == ["exec", "--help"]:
    print("--ephemeral --strict-config --skip-git-repo-check --sandbox --add-dir --output-last-message")
else:
    if os.environ.get("FLASH_STUB_READY"):
        Path(os.environ["FLASH_STUB_READY"]).write_text(str(os.getpid()))
        time.sleep(30)
    if "--output-last-message" in sys.argv:
        output = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
        if os.environ.get("FLASH_STUB_NO_FINAL"):
            output.unlink(missing_ok=True)
        else:
            output.write_text(os.environ.get("FLASH_STUB_FINAL", "STUB_OK\\n"))
    if os.environ.get("FLASH_STUB_CALLS"):
        with open(os.environ["FLASH_STUB_CALLS"], "a") as calls:
            calls.write("call\\n")
    print(json.dumps({"args": sys.argv[1:], "cwd": os.getcwd(),
                      "stdin": sys.stdin.read() if sys.argv[-1:] == ["-"] else None,
                      "codex_home": os.environ.get("CODEX_HOME"),
                      "apparmor": os.environ.get("FLASH_TEST_APPARMOR") == "1",
                      "openai_key_present": "OPENAI_API_KEY" in os.environ,
                      "codex_key_present": "CODEX_API_KEY" in os.environ,
                      "key_present": bool(os.environ.get("DEEPSEEK_API_KEY"))}))
    sys.exit(int(os.environ.get("FLASH_STUB_EXIT", "0")))
''')
    stub.chmod(0o755)
    apparmor = temp / "aa-exec"
    apparmor.write_text('''#!/usr/bin/env python3
import os, sys
if os.environ.get("FLASH_TEST_NO_APPARMOR"):
    sys.exit(1)
assert sys.argv[1:4] == ["-p", "codex-flash-worker", "--"]
os.environ["FLASH_TEST_APPARMOR"] = "1"
os.execvp(sys.argv[4], sys.argv[4:])
''')
    apparmor.chmod(0o755)
    copy = temp / "cp"
    copy.write_text('''#!/usr/bin/env python3
import os, sys
from pathlib import Path
if os.environ.get("FLASH_TEST_COPY_FAIL"):
    Path(sys.argv[-1]).write_text("PARTIAL_COPY")
    sys.exit(1)
os.execv("/usr/bin/cp", ["cp", *sys.argv[1:]])
''')
    copy.chmod(0o755)
    env = dict(os.environ, PATH=str(temp) + os.pathsep + os.environ["PATH"],
               DEEPSEEK_API_KEY="test-only-placeholder", OPENAI_API_KEY="test-openai",
               CODEX_API_KEY="test-codex")
    task = "Inspect literal $(touch SHOULD_NOT_EXIST) `touch ALSO_NOT_EXIST`\nReport only."
    result = subprocess.run(["bash", str(root / "flash-agent"), "--read-only", "--json",
                             "--output", "report with spaces.txt", task],
                            cwd=temp, env=env, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    args = data["args"]
    assert args[-2:] == ["--", task]
    assert args[args.index("--sandbox") + 1] == "read-only"
    result_dir = Path(args[args.index("--output-last-message") + 1]).parent
    assert result_dir.parent == Path(tempfile.gettempdir())
    assert result_dir.name.startswith("flash-result.")
    assert args[args.index("--add-dir") + 1] == str(result_dir)
    assert args[args.index("--model") + 1] == "deepseek-flash"
    assert args[args.index("--ask-for-approval") + 1] == "never"
    assert 'model_provider="deepseek"' in args
    assert 'model_providers.deepseek.base_url="https://api.deepseek.com/"' in args
    assert data["codex_home"] == str(Path.home() / ".codex-ds")
    assert data["cwd"] == str(temp) and data["key_present"]
    assert data["apparmor"]
    assert not data["openai_key_present"] and not data["codex_key_present"]
    assert (temp / "report with spaces.txt").read_text() == "STUB_OK\n"
    assert not Path(args[args.index("--output-last-message") + 1]).exists()
    assert not (temp / "SHOULD_NOT_EXIST").exists() and not (temp / "ALSO_NOT_EXIST").exists()
    # Invalid report destinations must fail before spending API tokens.
    link = temp / "report-link"
    link.symlink_to(temp / "report with spaces.txt")
    fifo = temp / "report-fifo"
    os.mkfifo(fifo)
    for destination in (temp, temp / "missing-parent" / "report.txt", link, fifo):
        calls = temp / "invalid-output-calls"
        invalid_output = subprocess.run(
            ["bash", str(root / "flash-agent"), "--output", str(destination), "Task"],
            env=dict(env, FLASH_STUB_CALLS=str(calls)), capture_output=True, text=True)
        assert invalid_output.returncode == 73, invalid_output
        assert "FLASH_OUTPUT_ERROR" in invalid_output.stderr
        assert not calls.exists()
    unavailable_temp = subprocess.run(["bash", str(root / "flash-agent"), "Task"],
                                     env=dict(env, TMPDIR=str(temp / "missing-tmp")),
                                     capture_output=True, text=True)
    assert unavailable_temp.returncode == 73
    assert "FLASH_OUTPUT_ERROR" in unavailable_temp.stderr and not unavailable_temp.stdout
    previous = temp / "previous-result.txt"
    previous.write_text("PREVIOUS_RESULT")
    failed_copy = subprocess.run(
        ["bash", str(root / "flash-agent"), "--output", str(previous), "Task"],
        env=dict(env, FLASH_TEST_COPY_FAIL="1"), capture_output=True, text=True)
    assert failed_copy.returncode == 73 and "FLASH_OUTPUT_ERROR" in failed_copy.stderr
    assert previous.read_text() == "PREVIOUS_RESULT"
    assert not list(temp.glob(".flash-output.*"))
    result = subprocess.run(["bash", str(root / "flash-agent"), "Implement delegated test."],
                            env=env, capture_output=True, text=True, check=True)
    args = json.loads(result.stdout)["args"]
    assert args[args.index("--sandbox") + 1] == "workspace-write"
    without_profile = subprocess.run(["bash", str(root / "flash-agent"), "Task"],
                                     env=dict(env, FLASH_TEST_NO_APPARMOR="1"),
                                     capture_output=True, text=True, check=True)
    assert not json.loads(without_profile.stdout)["apparmor"]
    piped = subprocess.run(["bash", str(root / "flash-agent"), "-"], input="Delegated stdin task\n",
                           env=env, capture_output=True, text=True, check=True)
    assert json.loads(piped.stdout)["stdin"] == "Delegated stdin task\n"
    # Progress output and an old report must not hide an empty final answer, even with exit 0.
    for case, extra in [("empty", {"FLASH_STUB_FINAL": ""}),
                        ("whitespace", {"FLASH_STUB_FINAL": " \t\n\u00a0"}),
                        ("missing", {"FLASH_STUB_NO_FINAL": "1"})]:
        previous = temp / "previous-result.txt"
        previous.write_text("PREVIOUS_RESULT")
        calls = temp / (case + "-calls")
        empty = subprocess.run(["bash", str(root / "flash-agent"), "--json", "--output", str(previous), "Task"],
                               env=dict(env, FLASH_STUB_CALLS=str(calls), **extra),
                               capture_output=True, text=True)
        assert empty.returncode == 70 and "FLASH_EMPTY_RESPONSE" in empty.stderr
        assert empty.stdout and previous.read_text() == "PREVIOUS_RESULT"
        assert calls.read_text() == "call\n"
        args = json.loads(empty.stdout)["args"]
        assert not Path(args[args.index("--output-last-message") + 1]).exists()
        assert not list(temp.glob(".flash-output.*"))
    smoke_empty = subprocess.run(["python3", str(root / "smoke-test.py")],
                                 env=dict(env, FLASH_STUB_NO_FINAL="1"),
                                 capture_output=True, text=True)
    assert smoke_empty.returncode == 70, smoke_empty
    assert json.loads(smoke_empty.stdout)["empty_response"] is True
    # Cancelling the wrapper must stop its child rather than leave a billable worker running.
    ready = temp / "worker-ready"
    process = subprocess.Popen(["bash", str(root / "flash-agent"), "Task"],
                               env=dict(env, FLASH_STUB_READY=str(ready)),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        child_pid = int(ready.read_text())
        process.terminate()
        process.communicate(timeout=5)
        assert process.returncode == 143
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError("Cancelled worker is still running")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
    env["FLASH_STUB_EXIT"] = "17"
    assert subprocess.run(["bash", str(root / "flash-agent"), "Task"], env=env,
                          capture_output=True).returncode == 17
    for invalid in ([], ["--output"], ["--dangerously-bypass-approvals-and-sandbox", "Task"], ["a", "b"]):
        assert subprocess.run(["bash", str(root / "flash-agent"), *invalid], env=env,
                              capture_output=True).returncode == 2
    env.pop("DEEPSEEK_API_KEY")
    missing = subprocess.run(["bash", str(root / "flash-agent"), "Task"], env=env,
                             capture_output=True, text=True)
    assert missing.returncode == 2 and "not exported" in missing.stderr
    assert not missing.stdout

    # Supervisor retains the normal home/auth, while worker tests above prove isolation.
    native_args = ["--yolo", "exec", "--json", "--", task]
    native = subprocess.run(["bash", str(root / "codex_ds"), *native_args],
                            cwd=temp, env=env, capture_output=True, text=True)
    assert native.returncode == 17
    native_data = json.loads(native.stdout)
    assert native_data["args"] == native_args and native_data["cwd"] == str(temp)
    assert native_data["codex_home"] == str(Path.home() / ".codex")
    assert native_data["apparmor"] and native_data["openai_key_present"]
    assert native_data["codex_key_present"]
    env["DEEPSEEK_API_KEY"] = "test-only-placeholder"
    native = subprocess.run(["bash", str(root / "codex_ds"), "--model", "gpt-5.6-sol"],
                            cwd=temp, env=env, capture_output=True, text=True)
    native_data = json.loads(native.stdout)
    assert native_data["args"] == ["--model", "gpt-5.6-sol"] and native_data["key_present"]

    # Direct DeepSeek supervisor: picked at launch, normal home/state kept, env scrubbed.
    ds_home = str(Path.home() / ".codex")
    ds_catalog = str(Path.home() / ".codex-ds" / "models.json")
    ds_overrides = ['model_provider="deepseek"',
                    'model_providers.deepseek.name="DeepSeek"',
                    'model_providers.deepseek.base_url="https://api.deepseek.com/"',
                    'model_providers.deepseek.wire_api="responses"',
                    'model_providers.deepseek.env_key="DEEPSEEK_API_KEY"',
                    'model_providers.deepseek.requires_openai_auth=false',
                    f'model_catalog_json="{ds_catalog}"',
                    'model_reasoning_effort="high"',
                    'plan_mode_reasoning_effort="high"',
                    'web_search="disabled"']
    ds_args = [token for value in ds_overrides for token in ("-c", value)]

    def run_launcher(arguments, extra_env=None):
        return subprocess.run(["bash", str(root / "codex_ds"), *arguments], cwd=temp,
                              env=dict(env, **(extra_env or {})), capture_output=True, text=True)

    # Every documented selector spelling routes; the rest of the command line stays literal.
    for selector in (["--model", "deepseek-flash"], ["-m", "deepseek-flash"],
                     ["--model=deepseek-flash"], ["-m=deepseek-flash"], ["-mdeepseek-flash"]):
        arguments = [*selector, "exec", "--json", "--", task]
        routed = run_launcher(arguments)
        assert routed.returncode == 17, routed.stderr
        routed_data = json.loads(routed.stdout)
        assert routed_data["args"] == ds_args + arguments, routed_data["args"]
        assert routed_data["codex_home"] == ds_home
        assert routed_data["key_present"] and not routed_data["openai_key_present"]
        assert not routed_data["codex_key_present"] and routed_data["apparmor"]
    assert not (temp / "SHOULD_NOT_EXIST").exists() and not (temp / "ALSO_NOT_EXIST").exists()
    # Native interactive, exec, and resume shapes are all supported.
    for arguments in (["exec", "--model", "deepseek-flash", "--json", "--", "Task"],
                      ["exec", "-c", "model=ignored", "--model", "deepseek-flash", "--json", "--", "Task"],
                      ["--model", "deepseek-flash", "resume", "--last"],
                      ["exec", "resume", "--last", "-m", "deepseek-flash"]):
        routed = run_launcher(arguments)
        assert routed.returncode == 17, routed.stderr
        routed_data = json.loads(routed.stdout)
        assert routed_data["args"] == ds_args + arguments and routed_data["key_present"]
        assert routed_data["codex_home"] == ds_home
    # Only a real selector routes: option values and text after `--` must stay on GPT.
    for arguments in (["exec", "--json", "--", "--model deepseek-flash"],
                      ["exec", "--profile", "deepseek-flash", "--json", "--", "Task"],
                      ["--profile", "deepseek-flash", "--model", "gpt-5.6-sol"],
                      ["-c", "model=deepseek-flash", "exec", "Task"],
                      ["exec", "--json", "--", "-m", "deepseek-flash"]):
        plain = run_launcher(arguments)
        assert plain.returncode == 17, plain.stderr
        plain_data = json.loads(plain.stdout)
        assert plain_data["args"] == arguments and plain_data["codex_home"] == ds_home
        assert plain_data["openai_key_present"] and plain_data["codex_key_present"]
    # The DeepSeek route must refuse to launch without the exported key.
    calls = temp / "direct-ds-calls"
    without_key = dict(env, FLASH_STUB_CALLS=str(calls))
    without_key.pop("DEEPSEEK_API_KEY")
    missing_key = subprocess.run(["bash", str(root / "codex_ds"), "--model", "deepseek-flash"],
                                 cwd=temp, env=without_key, capture_output=True, text=True)
    assert missing_key.returncode == 2
    assert "DEEPSEEK_API_KEY" in missing_key.stderr and not missing_key.stdout
    assert env["DEEPSEEK_API_KEY"] not in missing_key.stderr and not calls.exists()

    # Interactive/default launches hand off to the sibling model picker with a
    # real terminal, literal arguments, the normal ~/.codex home, and the same
    # optional AppArmor runner. The sibling's model-picker.py may not exist in
    # this checkout yet, so a local fixture stands in for it here.
    picker_home = temp / "picker-home"
    (picker_home / ".codex-ds").mkdir(parents=True)
    picker_script = picker_home / ".codex-ds" / "model-picker.py"
    picker_script.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
Path(os.environ["FLASH_TEST_PICKER_RECORD"]).write_text(json.dumps({
    "args": sys.argv[1:],
    "codex_home": os.environ.get("CODEX_HOME"),
    "apparmor": os.environ.get("FLASH_TEST_APPARMOR") == "1",
    "key_present": bool(os.environ.get("DEEPSEEK_API_KEY")),
    "openai_key_present": "OPENAI_API_KEY" in os.environ}))
sys.exit(int(os.environ.get("FLASH_TEST_PICKER_EXIT", "0")))
''')
    picker_script.chmod(0o755)

    def run_with_tty(arguments, extra_env=None):
        """Run codex_ds with a real PTY on stdin/stdout and return (code, out, err)."""
        master, slave = pty.openpty()
        try:
            process = subprocess.Popen(["bash", str(root / "codex_ds"), *arguments],
                                       cwd=temp, env=dict(env, **(extra_env or {})),
                                       stdin=slave, stdout=slave, stderr=subprocess.PIPE, text=True)
            os.close(slave)
            slave = -1
            output = []
            while True:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.append(chunk)
            stderr = process.stderr.read()
            returncode = process.wait(timeout=20)
        finally:
            if slave != -1:
                os.close(slave)
            os.close(master)
        return returncode, b"".join(output).decode("utf-8", "replace"), stderr

    for index, interactive in enumerate(([], ["resume", "--last"], ["fork", "--last"],
                                         ["--yolo"], ["--model", "gpt-5.6-sol"],
                                         ["resume", "--last", "--model", "gpt-5.6-sol"],
                                         ["an initial prompt", "--no-alt-screen"],
                                         ["-c", "model=deepseek-flash"],
                                         ["--", "--model", "deepseek-flash"])):
        record = temp / f"picker-{index}.json"
        calls = temp / f"picker-{index}.calls"
        code, output, stderr = run_with_tty(
            interactive, {"HOME": str(picker_home), "FLASH_TEST_PICKER_RECORD": str(record),
                          "FLASH_STUB_CALLS": str(calls)})
        assert code == 0, (interactive, code, stderr)
        assert not calls.exists(), interactive
        picked = json.loads(record.read_text())
        assert picked["args"] == interactive, picked
        assert picked["codex_home"] == str(picker_home / ".codex")
        assert picked["apparmor"] and picked["key_present"] and picked["openai_key_present"]
        assert not output.strip()
    # Starting on DeepSeek must still allow switching back through /model.
    record = temp / "picker-ds.json"
    code, output, stderr = run_with_tty(["--model", "deepseek-flash"],
                                        {"HOME": str(picker_home), "FLASH_TEST_PICKER_RECORD": str(record)})
    assert code == 0, stderr
    ds_data = json.loads(record.read_text())
    assert ds_data["args"] == ["--model", "deepseek-flash"]
    assert ds_data["codex_home"] == str(picker_home / ".codex") and ds_data["apparmor"]
    assert ds_data["key_present"] and ds_data["openai_key_present"]
    # User-specified connection config and non-interactive commands bypass the picker.
    for index, bypass in enumerate((["--remote", "ws://127.0.0.1:1"], ["--remote=ws://127.0.0.1:1"],
                                    ["--oss"], ["--local-provider", "ollama"],
                                    ["--local-provider=ollama"], ["--profile", "custom"],
                                    ["--profile=custom"], ["-p", "custom"], ["-pcustom"],
                                    ["exec", "--json", "Task"], ["exec", "resume", "--last"],
                                    ["--help"], ["-V"], ["login"], ["mcp", "list"])):
        record = temp / f"bypass-{index}.json"
        code, output, stderr = run_with_tty(
            bypass, {"HOME": str(picker_home), "FLASH_TEST_PICKER_RECORD": str(record)})
        assert code == 17, (bypass, code, stderr)
        assert not record.exists(), bypass
        bypassed = json.loads(output)
        assert bypassed["args"] == bypass and bypassed["codex_home"] == str(picker_home / ".codex")
    # A missing helper must not launch a different interface silently.
    bare_home = temp / "picker-missing-home"
    bare_home.mkdir()
    calls = temp / "fallback.calls"
    code, output, stderr = run_with_tty(
        [], {"HOME": str(bare_home), "FLASH_STUB_CALLS": str(calls)})
    assert code == 2 and not calls.exists() and not output.strip()
    assert "model-picker.py not found" in stderr

    target = temp / "target-home"
    normal = target / ".codex"
    normal.mkdir(parents=True)
    original = b"Existing instructions, preserved byte-for-byte.\r\nNo final newline"
    (normal / "AGENTS.md").write_bytes(original)
    (normal / "config.toml").write_bytes(b'model = "existing-model"\n')
    (normal / "auth.json").write_bytes(b'{"test": "unchanged"}\n')
    # Install from a copied setup so the sibling-owned picker files can be
    # stood in for with fixtures without touching their checkout paths.
    spec = importlib.util.spec_from_file_location("flash_install", root / "install.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    assert "model-picker.py" in installer.FILES and "test_model_picker.py" in installer.FILES
    setup = temp / "setup"
    setup.mkdir()
    for name in installer.FILES:
        if (root / name).exists():
            shutil.copy2(root / name, setup / name)
        else:
            (setup / name).write_text(f"FIXTURE {name}\n")
    # A missing bundle helper must fail before any file is written.
    broken = temp / "broken-setup"
    broken.mkdir()
    for name in installer.FILES:
        if name != "model-picker.py":
            shutil.copy2(setup / name, broken / name)
    broken_run = subprocess.run(["python3", str(broken / "install.py"),
                                 "--target-home", str(broken / "home")],
                                env=env, capture_output=True, text=True)
    assert broken_run.returncode != 0 and "Missing bundle file" in broken_run.stderr
    assert not (broken / "home" / ".codex" / "AGENTS.md").exists()
    install = ["python3", str(setup / "install.py"), "--target-home", str(target)]
    subprocess.run(install, env=env, capture_output=True, text=True, check=True)
    assert (normal / "AGENTS.md").read_bytes().startswith(original)
    backups = list(normal.glob("AGENTS.md.before-flash-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    first = (normal / "AGENTS.md").read_bytes()
    subprocess.run(install, env=env, capture_output=True, text=True, check=True)
    assert (normal / "AGENTS.md").read_bytes() == first
    assert len(list(normal.glob("AGENTS.md.before-flash-*"))) == 1
    assert (normal / "config.toml").read_bytes() == b'model = "existing-model"\n'
    assert (normal / "auth.json").read_bytes() == b'{"test": "unchanged"}\n'
    assert os.access(target / ".local/bin/flash-agent", os.X_OK)
    assert os.access(target / ".local/bin/codex_ds", os.X_OK)
    assert not (target / ".codex-ds/auth.json").exists()
    # The new picker helper and its offline check ship in the portable bundle.
    assert (target / ".codex-ds/model-picker.py").read_bytes() == (setup / "model-picker.py").read_bytes()
    assert os.access(target / ".codex-ds/model-picker.py", os.X_OK)
    assert (target / ".codex-ds/test_model_picker.py").read_bytes() == (setup / "test_model_picker.py").read_bytes()
    assert not os.access(target / ".codex-ds/test_model_picker.py", os.X_OK)
    assert not (target / ".local/bin/model-picker.py").exists()
    assert not (target / ".local/bin/test_model_picker.py").exists()
    # The installer must copy the changed launcher, and the installed copy must route.
    launcher = (root / "codex_ds").read_bytes()
    assert (target / ".codex-ds/codex_ds").read_bytes() == launcher
    assert (target / ".local/bin/codex_ds").read_bytes() == launcher
    installed = subprocess.run(["bash", str(target / ".local/bin/codex_ds"),
                                "--model", "deepseek-flash"],
                               cwd=temp, env=dict(env, HOME=str(target)),
                               capture_output=True, text=True)
    assert installed.returncode == 17, installed.stderr
    installed_data = json.loads(installed.stdout)
    assert installed_data["codex_home"] == str(target / ".codex")
    assert installed_data["args"][-2:] == ["--model", "deepseek-flash"]
    assert f'model_catalog_json="{target}/.codex-ds/models.json"' in installed_data["args"]
    assert 'model_provider="deepseek"' in installed_data["args"]
    assert installed_data["key_present"] and not installed_data["openai_key_present"]
    # Upgrade only our delimited policy; retain surrounding user text and back up the old file.
    old = (original + b"\n<!-- BEGIN DEEPSEEK FLASH WORKERS -->\nOld policy\n"
           b"<!-- END DEEPSEEK FLASH WORKERS -->\nUser instructions after the block.")
    (normal / "AGENTS.md").write_bytes(old)
    subprocess.run(install, env=env, capture_output=True, text=True, check=True)
    updated = (normal / "AGENTS.md").read_bytes()
    assert updated.startswith(original + b"\n")
    assert updated.endswith(b"\nUser instructions after the block.")
    assert (root / "astra-delegation.md").read_bytes().strip() in updated
    assert any(p.read_bytes() == old for p in normal.glob("AGENTS.md.before-flash-*"))
    # A conflict must fail before changing the worker setup.
    (normal / "AGENTS.md").write_bytes(b"<!-- BEGIN DEEPSEEK FLASH WORKERS -->\nDifferent policy\n")
    (target / ".codex-ds/config.toml").write_bytes(b"PRESERVE ON CONFLICT")
    conflict = subprocess.run(install, env=env, capture_output=True, text=True)
    assert conflict.returncode != 0
    assert (target / ".codex-ds/config.toml").read_bytes() == b"PRESERVE ON CONFLICT"
print("PASS: routing, stdin, literal prompts, empty/missing/blank answer blocking, no retries, output cleanup, backups, and portable install.")
