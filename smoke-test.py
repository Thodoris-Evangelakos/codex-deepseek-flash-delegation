#!/usr/bin/env python3
"""Exercise flash-agent in a temporary directory without persisting evidence."""
import datetime
import json
import os
import re
import secrets
import signal
import subprocess
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parent
launcher = root / "flash-agent"
report = {"checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
          "passed": False, "requested_model": "deepseek-flash"}
key = os.environ.get("DEEPSEEK_API_KEY")
if not key:
    report["error"] = "DEEPSEEK_API_KEY is not exported; no API call made."
else:
    with tempfile.TemporaryDirectory(prefix="flash-smoke-") as directory:
        marker = "FLASH_SMOKE_" + secrets.token_hex(8)
        Path(directory, "marker.txt").write_text(marker + "\n")
        answer_file = Path(directory, "final-answer.txt")
        prompt = ("Use a shell tool to read marker.txt in the current directory. "
                  "Make no changes. Reply with only its exact contents. "
                  "Do not inspect other paths, credentials, or environment variables.")
        try:
            process = subprocess.Popen([str(launcher),
                                     "--read-only", "--output", str(answer_file), prompt], cwd=directory,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, start_new_session=True)
            stdout, raw_stderr = process.communicate(timeout=180)
            # Never persist raw subprocess output or the environment.
            stderr = raw_stderr.replace(key, "[REDACTED]")
            fields = {}
            for field in ("model", "provider", "sandbox", "approval"):
                match = re.search(r"^" + field + r": (.+)$", stderr, re.MULTILINE)
                fields[field] = match.group(1).strip() if match else None
            report.update(fields)
            report["exit_code"] = process.returncode
            answer = answer_file.read_text().strip() if answer_file.exists() else ""
            report["read_temporary_marker"] = answer == marker
            report["passed"] = (process.returncode == 0 and report["read_temporary_marker"]
                                and fields == {"model": "deepseek-flash", "provider": "deepseek",
                                               "sandbox": "read-only", "approval": "never"})
            if not report["passed"]:
                report["final_answer"] = answer.replace(key, "[REDACTED]")[:500]
                # Only bounded, redacted error lines; never print whole tool transcripts.
                report["errors"] = [line[:500] for line in stderr.splitlines()
                                    if line.startswith(("ERROR:", "Error:", "warning:", "FLASH_EMPTY_RESPONSE:"))][-8:]
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            report["error"] = "Worker exceeded 180 seconds; its process group was stopped."
print(json.dumps(report, indent=2))
raise SystemExit(0 if report["passed"] else 1)
