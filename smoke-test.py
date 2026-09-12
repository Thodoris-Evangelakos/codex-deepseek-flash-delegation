#!/usr/bin/env python3
"""Exercise flash-agent in a temporary directory without persisting evidence."""
import argparse
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

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--workspace-write", action="store_true",
                   help="Use the launcher's default workspace-write sandbox and require the "
                        "worker to create worker-written.txt with apply_patch. Default: read-only.")
arguments = parser.parse_args()
read_only = not arguments.workspace_write
expected_sandbox = "read-only" if read_only else "workspace-write"

report = {"checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
          "passed": False, "requested_model": "deepseek-flash",
          "expected_sandbox": expected_sandbox}
status = 1
key = os.environ.get("DEEPSEEK_API_KEY")
if not key:
    report["error"] = "DEEPSEEK_API_KEY is not exported; no API call made."
else:
    with tempfile.TemporaryDirectory(prefix="flash-smoke-") as directory:
        marker = "FLASH_SMOKE_" + secrets.token_hex(8)
        Path(directory, "marker.txt").write_text(marker + "\n")
        answer_file = Path(directory, "final-answer.txt")
        written_file = Path(directory, "worker-written.txt")
        if read_only:
            prompt = ("Use a shell tool to read marker.txt in the current directory. "
                      "Make no changes. Reply with only its exact contents. "
                      "Do not inspect other paths, credentials, or environment variables.")
        else:
            prompt = ("Use a shell tool to read marker.txt in the current directory. "
                      "Then use apply_patch to create worker-written.txt in the current "
                      "directory whose contents are exactly that marker text. "
                      "Make no other changes. Reply with only the exact contents of "
                      "marker.txt. Do not inspect other paths, credentials, or "
                      "environment variables.")
        command = [str(launcher)]
        if read_only:
            command.append("--read-only")
        command += ["--output", str(answer_file), prompt]
        try:
            process = subprocess.Popen(command, cwd=directory,
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
            if read_only:
                banner_matches_mode = fields["sandbox"] == expected_sandbox
            else:
                banner_matches_mode = bool(fields["sandbox"]
                                           and fields["sandbox"].startswith("workspace-write"))
            report["sandbox_matches_mode"] = banner_matches_mode
            answer = answer_file.read_text().strip() if answer_file.exists() else ""
            report["read_temporary_marker"] = answer == marker
            if read_only:
                report["wrote_worker_file"] = None
                wrote_worker_file = True
            else:
                wrote_worker_file = (written_file.is_file()
                                     and written_file.read_bytes() == (marker + "\n").encode())
                report["wrote_worker_file"] = wrote_worker_file
            fields_match = (fields["model"] == "deepseek-flash" and fields["provider"] == "deepseek"
                            and fields["approval"] == "never" and banner_matches_mode)
            report["passed"] = (process.returncode == 0 and report["read_temporary_marker"]
                                and wrote_worker_file and fields_match)
            # A missing final answer is a distinct outcome, not a generic failure.
            empty_response = process.returncode == 70 or "FLASH_EMPTY_RESPONSE" in stderr
            report["empty_response"] = empty_response
            if not report["passed"]:
                report["final_answer"] = answer.replace(key, "[REDACTED]")[:500]
                # Only bounded, redacted error lines; never print whole tool transcripts.
                report["errors"] = [line[:500] for line in stderr.splitlines()
                                    if line.startswith(("ERROR:", "Error:", "warning:", "FLASH_EMPTY_RESPONSE:", "FLASH_OUTPUT_ERROR:"))][-8:]
            status = 70 if empty_response else (0 if report["passed"] else 1)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            report["error"] = "Worker exceeded 180 seconds; its process group was stopped."
            status = 1
print(json.dumps(report, indent=2))
raise SystemExit(status)
