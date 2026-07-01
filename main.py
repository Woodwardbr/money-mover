import uvicorn

from money_mover.config import settings


def run() -> None:
    uvicorn.run(
        "money_mover.app:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
    )


if __name__ == "__main__":
    run()
