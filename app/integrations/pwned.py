"""Leaked-password check against the Have I Been Pwned "Pwned Passwords" range API.

k-anonymity: only the first five characters of the password's SHA-1 hash are
sent. The service answers with every leaked hash that starts with them, and
the comparison happens here. Responses are padded so their size does not hint
at the prefix. The password itself never leaves the server.

If the service cannot be reached the check is skipped (fail open) and logged:
a third-party outage must not stop people from signing up.
"""

import hashlib
import logging

import httpx

logger = logging.getLogger(__name__)

RANGE_URL = "https://api.pwnedpasswords.com/range/{prefix}"


def times_leaked(password: str, *, client: httpx.Client | None = None, timeout: float = 3.0) -> int | None:
    """How often the password appears in known breaches; None when the service could not be asked."""
    digest = hashlib.sha1(password.encode("utf-8"), usedforsecurity=False).hexdigest().upper()
    prefix, suffix = digest[:5], digest[5:]
    owned = client is None
    http = client or httpx.Client(timeout=timeout)
    try:
        response = http.get(
            RANGE_URL.format(prefix=prefix),
            headers={"Add-Padding": "true", "User-Agent": "NOVALYN-password-check"},
        )
        response.raise_for_status()
    except httpx.HTTPError:
        logger.warning("Leaked-password check skipped: the service could not be reached")
        return None
    finally:
        if owned:
            http.close()
    for line in response.text.splitlines():
        candidate, _, count = line.strip().partition(":")
        if candidate == suffix:
            return int(count or 0)  # padding entries have a count of 0
    return 0
