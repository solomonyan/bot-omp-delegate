#!/usr/bin/env python3
"""Send one signed disk.low webhook without contacting the delegate daemon."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


HERE = Path(__file__).resolve().parent
for package_root in (HERE, HERE.parents[1] if len(HERE.parents) > 1 else HERE):
    if (package_root / "bot_omp_delegate").is_dir():
        sys.path.insert(0, str(package_root))
        break

from bot_omp_delegate.webhooks import ConfigError, WebhookDispatcher


def main(argv=None):
    parser = argparse.ArgumentParser(description="Emit a signed disk.low event to the configured disk subscriber.")
    parser.add_argument("--threshold-gb", required=True, type=float)
    parser.add_argument("--free-gb", required=True, type=float)
    args = parser.parse_args(argv)

    state = Path(os.environ.get("OMP_DELEGATE_STATE", "~/.omp-delegate")).expanduser()
    dispatcher = WebhookDispatcher(state)
    try:
        result = dispatcher.emit_disk_low(free_gb=args.free_gb, threshold_gb=args.threshold_gb)
    except (ConfigError, OSError):
        print(json.dumps({"error": "disk.low delivery unavailable"}), file=sys.stderr)
        return 1
    finally:
        dispatcher.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["delivered"] == result["configured"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
