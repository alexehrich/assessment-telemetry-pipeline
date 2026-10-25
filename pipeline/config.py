"""Reads configuration from environment variables, so no node is hardcoded."""

import os


def env(name: str, default=None, cast=str):
    value = os.environ.get(name)
    if value is None or value == "":
        if default is None:
            raise SystemExit(f"missing required environment variable {name}")
        return default
    try:
        return cast(value)
    except ValueError:
        raise SystemExit(f"invalid value for {name}: {value!r}") from None


def host_port(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(value)
    return host, int(port)
