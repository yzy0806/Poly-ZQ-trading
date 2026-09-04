from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

GENERATED_VALUES = {
    "DASHBOARD_PASSWORD": lambda: secrets.token_urlsafe(32),
    "SESSION_SIGNING_KEY": lambda: secrets.token_hex(32),
    "CONTROL_CONFIRMATION_SECRET": lambda: secrets.token_urlsafe(32),
}


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: bootstrap_env.py SOURCE_EXAMPLE TARGET_ENV", file=sys.stderr)
        return 2
    source = Path(sys.argv[1])
    target = Path(sys.argv[2])
    if target.exists():
        print(f"refusing to overwrite existing environment: {target}", file=sys.stderr)
        return 1

    lines: list[str] = []
    seen: set[str] = set()
    for line in source.read_text(encoding="utf-8").splitlines():
        key, separator, _ = line.partition("=")
        if separator and key in GENERATED_VALUES:
            line = f"{key}={GENERATED_VALUES[key]()}"
            seen.add(key)
        lines.append(line)
    missing = set(GENERATED_VALUES) - seen
    if missing:
        print(f"example is missing generated keys: {', '.join(sorted(missing))}", file=sys.stderr)
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    print(f"created {target} with mode 0600")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
