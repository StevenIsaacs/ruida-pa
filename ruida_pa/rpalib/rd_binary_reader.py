"""
RdBinaryStream — binary stream reader for RDWorks .rd files.

Provides unswizzled bytes to the Ruida protocol parser from RDWorks
binary export files (.rd extension).
"""

from ruida_pa.rpalib.rpa_swizzler import RpaSwizzler


class RdBinaryStream:
    """Reads .rd binary files and provides unswizzled bytes to the parser.

    .rd file format:
    - 10-byte header starting with b"RDWORKV" (7 known + 3 wildcard bytes)
    - The header is NOT swizzled
    - Remaining bytes form a continuous swizzled byte stream
      (USB transport format: swizzled, no ACK/NAK, no packet checksum,
      no packet boundaries).
    """

    HEADER_MAGIC = b"RDWORKV"
    HEADER_LEN = 10

    def __init__(self, path: str, magic: int = 0x88):
        self._path = path
        self._pos = 0
        self._total = 0
        self._data: bytearray = bytearray()
        self._read_and_unswizzle(magic)

    def _read_and_unswizzle(self, magic: int) -> None:
        with open(self._path, "rb") as f:
            raw = f.read()
        # Test for RDWORKV header: if present, skip it; otherwise use raw as-is
        if len(raw) < self.HEADER_LEN:
            # Too small for a header — whole file is swizzled data
            swizzled = bytearray(raw)
        elif raw[:7] == self.HEADER_MAGIC:
            swizzled = bytearray(raw[self.HEADER_LEN :])
        else:
            # No RDWORKV header — treat entire file as swizzled byte stream
            swizzled = bytearray(raw)
        if not swizzled:
            raise ValueError(f"Empty payload in RD file: {self._path}")
        swizzler = RpaSwizzler(magic=magic)
        self._data = swizzler.unswizzle(swizzled)
        self._total = len(self._data)

    def next_byte(self) -> int | None:
        if self._pos >= self._total:
            return None
        b = self._data[self._pos]
        self._pos += 1
        return b

    @property
    def take(self) -> int:
        return self._pos

    @property
    def remaining(self) -> int:
        return self._total - self._pos
