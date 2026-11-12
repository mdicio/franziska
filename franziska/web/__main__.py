"""uv run --env-file .env python -m franziska.web: the web UI on FRANZISKA_WEB_HOST:FRANZISKA_WEB_PORT."""

import ipaddress

import pydantic_ai
import uvicorn

from franziska import obs
from franziska.config import Settings
from franziska.web.app import create_app


def main() -> None:
    pydantic_ai.BANNER_ENABLED = False
    settings = Settings.from_env()
    obs.setup("franziska-web", settings.traces_db, settings.otlp)
    try:
        loopback = ipaddress.ip_address(settings.web_host).is_loopback
    except ValueError:
        loopback = settings.web_host == "localhost"
    if not loopback and not settings.web_token:
        raise SystemExit(f"FRANZISKA_WEB_HOST={settings.web_host} needs FRANZISKA_WEB_TOKEN (see .env.example).")
    # One worker: the conversation, its proposals and the GPU turn order live in this process.
    uvicorn.run(create_app(settings), host=settings.web_host, port=settings.web_port, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1", log_level="info")


if __name__ == "__main__":
    main()
