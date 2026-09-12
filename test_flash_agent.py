#!/usr/bin/env python3
"""Offline regression check: argument safety, isolation, failures, and backup preservation."""
import json
import os
import subprocess
import tempfile
import time
import signal
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
    result_dir = Path(args[args.index("--add-dir") + 1])
    assert result_dir.parent == Path(tempfile.gettempdir())
    assert result_dir.name.startswith("flash-result.")
    assert Path(args[args.index("--output-last-message") + 1]).parent == result_dir
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

    target = temp / "target-home"
    normal = target / ".codex"
    normal.mkdir(parents=True)
    original = b"Existing instructions, preserved byte-for-byte.\r\nNo final newline"
    (normal / "AGENTS.md").write_bytes(original)
    (normal / "config.toml").write_bytes(b'model = "existing-model"\n')
    (normal / "auth.json").write_bytes(b'{"test": "unchanged"}\n')
    install = ["python3", str(root / "install.py"), "--target-home", str(target)]
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
