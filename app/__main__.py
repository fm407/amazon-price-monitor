"""Container entry point: `python -m app`."""

import uvicorn

if __name__ == "__main__":
    # One worker only: the scheduler lives in this process (see docs/ARCHITECTURE.md).
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 - container-internal; the host binding is set in compose.yaml
        port=8080,
        workers=1,
        log_config=None,
        server_header=False,
        timeout_graceful_shutdown=20,
    )
