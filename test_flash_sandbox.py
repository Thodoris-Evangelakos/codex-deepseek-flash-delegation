#!/usr/bin/env python3
"""Check real Codex filesystem enforcement without an API call or model."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

runner = []
if shutil.which("aa-exec") and subprocess.run(
    ["aa-exec", "-p", "codex-flash-worker", "--", "/usr/bin/true"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
).returncode == 0:
    runner = ["aa-exec", "-p", "codex-flash-worker", "--"]

# Outside /tmp: workspace profiles intentionally allow writes throughout /tmp.
with tempfile.TemporaryDirectory(prefix="flash-sandbox-", dir=Path.home()) as directory:
    fixture = Path(directory)
    workspace = fixture / "workspace"
    workspace.mkdir()
    runtime = fixture / "runtime"
    runtime.mkdir()
    env = dict(os.environ, CODEX_HOME=str(runtime))
    probe = """
import errno, sys
from pathlib import Path
for name, allowed in [("inside.txt", sys.argv[1] == ":workspace"), ("../outside.txt", False)]:
    try:
        Path(name).write_text("sandbox probe")
    except OSError as error:
        assert not allowed and error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), error
    else:
        assert allowed, "Unexpected write access: " + name
print("PASS:", sys.argv[1], "workspace access and outside-workspace write denial")
"""
    for profile in (":workspace", ":read-only"):
        subprocess.run(
            runner + ["codex", "sandbox", "-P", profile, "-C", str(workspace),
                      "--", sys.executable, "-c", probe, profile],
            env=env, check=True, timeout=30,
        )
    # An inherited read-only filesystem cannot be widened by the worker's flags.
    blocked = subprocess.run(
        runner + ["codex", "sandbox", "-P", ":read-only", "-C", str(workspace),
                  "--", "bash", str(Path(__file__).with_name("flash-agent")),
                  "--read-only", "This preflight must fail before starting a worker."],
        env=dict(env, DEEPSEEK_API_KEY="offline-placeholder", CODEX_DS_SUPERVISOR="1"),
        capture_output=True, text=True, timeout=30,
    )
    assert blocked.returncode == 73, blocked.stderr
    assert "FLASH_OUTPUT_ERROR" in blocked.stderr and not blocked.stdout
    print("PASS: inherited read-only filesystem fails before a worker/API call")
