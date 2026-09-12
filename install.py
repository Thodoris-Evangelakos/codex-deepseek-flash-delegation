#!/usr/bin/env python3
"""Install the reviewed bundle; preserve normal Codex config and authentication."""
import argparse
import datetime
import shutil
import subprocess
from pathlib import Path

FILES = ("config.toml", "models.json", "AGENTS.md", "flash-agent", "astra-delegation.md",
         "install.py", "test_flash_agent.py", "smoke-test.py", "README.md",
         "codex-flash-worker.apparmor", "codex_ds")


def install(target_home):
    source = Path(__file__).resolve().parent
    worker = target_home / ".codex-ds"
    normal = target_home / ".codex"
    if worker.resolve() == normal.resolve():
        raise SystemExit("Worker home must be separate from normal Codex home.")
    cli = shutil.which("codex")
    if not cli:
        raise SystemExit("Install Codex CLI and put it on PATH first. This installer does not install Codex.")
    help_text = subprocess.check_output([cli, "exec", "--help"], text=True)
    for flag in ("--ephemeral", "--strict-config", "--skip-git-repo-check", "--sandbox", "--add-dir", "--output-last-message"):
        if flag not in help_text:
            raise SystemExit(f"Installed Codex is missing {flag}; update Codex before installing.")
    # Check the normal-home edit before making any installation changes.
    agents = normal / "AGENTS.md"
    if agents.is_symlink():
        raise SystemExit(f"Refusing to overwrite a symlink: {agents}")
    existing = agents.read_bytes() if agents.exists() else b""
    section = (source / "astra-delegation.md").read_bytes()
    begin = b"<!-- BEGIN DEEPSEEK FLASH WORKERS -->"
    end = b"<!-- END DEEPSEEK FLASH WORKERS -->"
    has_section = begin in existing
    if has_section or end in existing:
        if existing.count(begin) != 1 or existing.count(end) != 1 or existing.index(begin) > existing.index(end):
            raise SystemExit("Malformed delegation markers; preserved all files for manual review.")
        updated_agents = (existing[:existing.index(begin)] + section.rstrip(b"\n")
                          + existing[existing.index(end) + len(end):])
    else:
        updated_agents = existing + (b"\n\n" if existing else b"") + section
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")

    def write(path, data, mode=0o600):
        if path.is_symlink():
            raise SystemExit(f"Refusing to overwrite a symlink: {path}")
        if path.exists() and path.read_bytes() == data:
            if mode == 0o755:
                path.chmod(mode)
            print(f"Unchanged: {path}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = path.with_name(path.name + ".before-flash-" + stamp)
            shutil.copy2(path, backup)
            print(f"Backup: {backup}")
        path.write_bytes(data)
        path.chmod(mode)
        print(f"Wrote: {path}")

    payload = {name: (source / name).read_bytes() for name in FILES}
    for name, data in payload.items():
        write(worker / name, data, 0o755 if name in ("flash-agent", "codex_ds") else 0o600)
    for name in ("flash-agent", "codex_ds"):
        write(target_home / ".local/bin" / name, payload[name], 0o755)
    if not agents.exists():
        print("Normal AGENTS.md did not exist; no prior content to back up.")
    write(agents, updated_agents)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-home", type=Path, default=Path.home())
    install(parser.parse_args().target_home.expanduser().resolve())
