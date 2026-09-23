"""Portable single-worker launcher; network exposure requires explicit opt-in."""

import os

import uvicorn

from .app import LOCAL_HOSTS, allowed_hosts


def main():
    host = os.environ.get("HACKALEM_BIND_HOST", "127.0.0.1")
    if host not in LOCAL_HOSTS and not (allowed_hosts() - LOCAL_HOSTS):
        raise SystemExit("Для внешнего bind задайте точный домен в HACKALEM_ALLOWED_HOSTS.")
    try:
        port = int(os.environ.get("PORT", "8765"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise SystemExit("PORT должен быть целым числом от 1 до 65535.") from None
    # Results live in this process: multiple workers would lose result_id lookups.
    uvicorn.run("smartbuyer.app:app", host=host, port=port, workers=1)


if __name__ == "__main__":
    main()
