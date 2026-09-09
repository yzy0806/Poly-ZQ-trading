"""Run with PYTHONPATH=src; output contains venue bodies with credentials redacted."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from pydantic import ValidationError

from zq_arb.auth_diagnostic import check_auth
from zq_arb.config import Settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--include-history", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("runtime/polymarket-auth-check.json"))
    args = parser.parse_args()
    try:
        settings = Settings(_env_file=args.env_file)
    except ValidationError as error:
        # Never print Pydantic input values or validator context containing secrets.
        print(
            json.dumps(
                {
                    "configuration_errors": [
                        {"field": ".".join(map(str, item["loc"])), "type": item["type"]}
                        for item in error.errors(include_input=False, include_context=False)
                    ]
                },
                indent=2,
            )
        )
        return 1
    report = asyncio.run(check_auth(settings, include_history=args.include_history))
    rendered = json.dumps(report, indent=2)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    checks = ["test_2", "test_3"]
    if args.include_history:
        checks += ["trade_history", "wallet_activity"]
    return 0 if all(report[key]["status"] == "PASS" for key in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
