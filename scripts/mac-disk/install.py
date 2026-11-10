#!/usr/bin/env python3
"""Install local disk tools; reaper is always installed in dry-run mode."""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess


def main():
    source = Path(__file__).resolve().parent
    home = Path.home()
    target = home / "lanes/omp-runs/live/bin/disk"
    data = home / "lanes/omp-runs/disk-prevention"
    agents = home / "Library/LaunchAgents"
    for folder in (target, data, agents):
        folder.mkdir(parents=True, exist_ok=True)
    os.chmod(data, 0o700)
    for script in source.glob("*.py"):
        if script.name != "install.py":
            shutil.copy2(script, target / script.name)
    # Private snapshot of the outbound library; no import from or edit to the
    # production checkout, and no daemon lifecycle commands.
    package = target / "bot_omp_delegate"
    package.mkdir(exist_ok=True)
    for name in ("__init__.py", "webhooks.py"):
        shutil.copy2(source.parents[1] / "bot_omp_delegate" / name, package / name)
    jobs = {
        "com.solomon.mac-disk-reaper": (["mac_disk_reaper.py", "--dry-run"], {"StartInterval": 3600}),
        "com.solomon.mac-disk-watch": (["mac_disk_ops.py", "watch"], {"StartInterval": 600}),
        # Daily scheduling is intentional: after an active-job skip, retry the
        # next day. weekly-state.json limits successful cleanup to once/7 days.
        "com.solomon.mac-disk-weekly": (["mac_disk_ops.py", "weekly"],
                                         {"StartCalendarInterval": {"Hour": 3, "Minute": 15}}),
    }
    domain = f"gui/{os.getuid()}"
    for label, (args, schedule) in jobs.items():
        path = agents / f"{label}.plist"
        payload = {"Label": label, "ProgramArguments": ["/usr/bin/env", "python3", str(target / args[0]), *args[1:]],
                   "StandardOutPath": str(data / f"{label}.out"),
                   "StandardErrorPath": str(data / f"{label}.err"),
                   "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
                   **schedule}
        path.write_bytes(plistlib.dumps(payload))
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True, capture_output=True)
        print(f"installed {label}: {' '.join(args)}")


if __name__ == "__main__":
    main()
