from __future__ import annotations

import uvicorn

from zq_arb.api import create_app
from zq_arb.config import get_settings


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
        workers=1,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    run()
