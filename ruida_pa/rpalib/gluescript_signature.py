"""SHA-256 signature of a gluescript transcript.

The signature lets a client verify that the transcript staged on the
server matches the local transcript without transferring the whole
transcript back over the wire: both sides hash the same lines and the
client compares digests, reading the full transcript back only when the
digests differ.
"""

import hashlib


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
