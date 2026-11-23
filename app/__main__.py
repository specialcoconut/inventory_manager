"""Run the kiosk server: ``python -m app`` (honours APP_PORT)."""

import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run("app.main:create_app", factory=True, host="0.0.0.0", port=settings.app_port, proxy_headers=True)


if __name__ == "__main__":
    main()
