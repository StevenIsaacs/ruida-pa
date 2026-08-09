"""SHA-256 signature of a gluescript transcript.

The signature lets a client verify that the transcript staged on the
server matches the local transcript without transferring the whole
transcript back over the wire: both sides hash the same lines and the
client compares digests, reading the full transcript back only when the
digests differ.
"""

import hashlib
import math


class GlueScriptDeltaMismatchError(RuntimeError):
    """Raised when a delta stage cannot be applied because the server
    transcript length does not match the client's flushed count
    (contiguity broken)."""


def gluescript_signature(lines: list[str]) -> str:
    """Return the SHA-256 hex digest of a gluescript transcript.

    The digest is computed incrementally, so hashing a large transcript
    never builds a giant joined string — O(total bytes) in time and
    O(1) steady-state extra memory (transient per-line allocations
    aside). SHA-256 gives a high probability of detecting
    any difference between two transcripts, and the length and per-line
    length prefixing makes reordering, insertion, and removal visible.

    Note that ``len()`` counts characters, not bytes — the encoding
    boundary here is ``utf-8``. A future byte-oriented scheme must not
    silently diverge between client and server; keep the hashing
    contract identical on both sides.

    Args:
        lines: The gluescript transcript lines, in order.

    Returns:
        The lowercase hex SHA-256 digest of the transcript.
    """
    h = hashlib.sha256()
    h.update(f"{len(lines)}\n".encode("utf-8"))
    for line in lines:
        h.update(f"{len(line)}\n{line}\n".encode("utf-8"))
    return h.hexdigest()


def is_valid_time_value(value) -> bool:
    """Return True iff value is acceptable as a DELAY/WAIT time token.

    Single source of truth shared by the direct driver
    (rd_gluescript._format_time_token) and the RPC client mirror
    (rpyc_client). Acceptance: bool rejected; numeric (int/float)
    accepted iff > 0 and finite (huge int whose float() conversion
    raises OverflowError counts as non-finite); str accepted iff
    stripped non-empty and the whitespace-compacted form ends with
    's' or 'ms' and its numeric part parses to a positive finite float.
    """
    if isinstance(value, bool):
        # bool is an int subclass — reject it explicitly.
        return False
    if isinstance(value, (int, float)):
        if value <= 0:
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            # int too large to convert to float — treat as non-finite
            return False
    if isinstance(value, str):
        token = value.strip()
        if not token:
            return False
        # Checks the same unit-suffixed format _parse_timeout accepts;
        # only sign and finiteness are checked here, so no unit
        # conversion is needed (500ms stays 500, whereas _parse_timeout
        # divides ms by 1000).
        compact = "".join(token.split())
        if compact.endswith("ms"):
            numeric = compact[:-2]
        elif compact.endswith("s"):
            numeric = compact[:-1]
        else:
            return False
        try:
            seconds = float(numeric)
        except ValueError:
            return False
        return math.isfinite(seconds) and seconds > 0
    return False
