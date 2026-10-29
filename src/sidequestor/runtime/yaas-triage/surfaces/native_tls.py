"""OS-managed certificate trust for HTTPS surfaces that need native roots."""

import ssl


def native_ssl_context():
    # Slack shares client.py but retains its existing TLS path.
    import truststore

    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
