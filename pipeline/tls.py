"""Mutual-TLS contexts. Both sides require TLS 1.3 and a certificate signed by the pipeline CA."""

import ssl


def server_context(ca: str, cert: str, key: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_cert_chain(cert, key)
    ctx.load_verify_locations(ca)
    ctx.verify_mode = ssl.CERT_REQUIRED  # no client certificate, no connection
    return ctx


def client_context(ca: str, cert: str, key: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # verifies the server cert and its hostname
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_verify_locations(ca)  # trust only the pipeline CA, not the system store
    ctx.load_cert_chain(cert, key)
    return ctx


def peer_common_name(ssl_object) -> str | None:
    """Returns the CN of the verified peer certificate, or None."""
    cert = ssl_object.getpeercert() if ssl_object else None
    for rdn in (cert or {}).get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return value
    return None
