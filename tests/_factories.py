"""Shared test factories for constructing realistic exception chains."""

import ssl

import httpx


def make_ssl_connect_error(
    msg: str = "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed",
) -> httpx.ConnectError:
    """Build an httpx.ConnectError with a realistic SSL cause chain.

    Mirrors the real chain: httpx.ConnectError -> httpcore.ConnectError -> ssl.SSLError.
    """
    ssl_err = ssl.SSLCertVerificationError(1, msg)
    core_err = Exception(msg)
    core_err.__context__ = ssl_err
    connect_err = httpx.ConnectError(msg)
    connect_err.__cause__ = core_err
    return connect_err


def make_connect_error(msg: str = "Connection refused") -> httpx.ConnectError:
    """Build an httpx.ConnectError with a realistic non-SSL cause chain."""
    os_err = OSError(msg)
    core_err = Exception(msg)
    core_err.__context__ = os_err
    connect_err = httpx.ConnectError(msg)
    connect_err.__cause__ = core_err
    return connect_err
