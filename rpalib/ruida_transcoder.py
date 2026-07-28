"""
Encoding and decoding of Ruida protocol binary data.

This module provides the RdDecoder (moved from protocols/ruida/ruida_parser.py)
and RdEncoder classes for converting between binary Ruida data and Python values.
"""

import protocols.ruida.ruida_protocol as rdap
from rpalib.rpa_emitter import RpaEmitter


class RdDecoder:
    """A parameter or reply decoder.

    Data representing a command parameter or reply. This also includes a
    state machine for accumulating and decoding a parameter. The step machine
    is a single state and is intended to run as a sub-state machine from the
    command parser state machine.

    NOTE: All incoming data bytes are expected to be 7 bit.

    To use prime the decoder with a parameter spec then step with incoming
    bytes until a value is returned or an error occurs.

    Attributes:
        format  The format string used to output the parameter data.
        rd_type The Ruida basic type for the parameter.
        decoder The decoder to call to convert the parameter bytes to
                a Python variable.
        accumulating
                True when accumulating parameter data.
        datum   The latest data byte.
        cstring True when accumulating a C formatted string (null terminated)
        data    The parameter data byte array to be converted.
        value   The resulting parameter value after conversion.
        checksum
                The result of the rd_checksum decoder. This is reset by the
                parser.
    """

    def __init__(self, output: RpaEmitter | None = None):
        self.out = output
        self.accumulating = False
        self.format: str = ""
        self.decoder: str = ""
        self.rd_type: str = ""
        self.datum = None
        self.data: bytearray = bytearray([])
        self.value = None  # The actual type is not known until after decode.
        self.cstring = False  # True when accumulating a cstring.
        self.checksum = 0
        self.file_checksum = 0
        self._rd_decoder = None
        self._length = 0
        self._remaining = 0

    @property
    def formatted(self) -> str:
        return self.format.format(self.value)

    @property
    def raw(self) -> bytearray:
        return self.data

    # ++++++++++++++
    # Decoders
    # Basic Types
    def rd_int7(self, data: bytearray):
        self.value = self.to_int(data)
        return self.formatted

    def rd_uint7(self, data: bytearray):
        self.value = data[0]
        return self.formatted

    def rd_bool(self, data: bytearray):
        self.value = data[0] != 0
        return self.formatted

    def to_int(self, data: bytearray, n_bytes=0) -> int:
        if not n_bytes:
            _n = self._length
        else:
            _n = n_bytes
        _v = 0
        _m = 0

        # TODO: This is a workaround and masks a problem with LightBurn.
        if _n == 5 and not data[0] & 0x40 and data[0] & 0x08:
            self.out.warn("LightBurn 35 bit signed integer WORKAROUND.")
            data[0] |= 0x70

        for _i in range(_n):
            _b = data[_i]
            if _i == 0:
                _b &= 0x3F
            _v = (_v << 7) + _b
            _m = (_m << 7) + 0x7F
        if data[0] & 0x40:
            _v = ((~_v & (_m >> 1)) + 1) * -1

        return _v

    def to_uint(self, data: bytearray, n_bytes=0) -> int:
        if not n_bytes:
            _n = self._length
        else:
            _n = n_bytes
        _v = 0
        for _i in range(_n):
            _v = (_v << 7) + data[_i]
        return _v

    def rd_int14(self, data: bytearray) -> int:
        self.value = self.to_int(data)
        return self.formatted

    def rd_uint14(self, data: bytearray) -> int:
        self.value = self.to_uint(data)
        return self.formatted

    def rd_int35(self, data: bytearray) -> int:
        self.value = self.to_int(data)
        return self.formatted

    def rd_uint35(self, data: bytearray) -> int:
        self.value = self.to_uint(data)
        return self.formatted

    def rd_cstring(self, data: bytearray):
        _i = 0
        _s = ""
        _na = False
        while True:
            if _i >= len(data):
                self.out.error("End of string not found.")
                break
            _c = data[_i]
            if _c == 0:
                break
            _s += chr(_c)
            if not _s.isprintable():
                _na = True
            _i += 1
        if _na:
            self.out.error(f"Non-printable characters in string: {data}")
        self.value = _s
        return self.formatted

    def rd_string8(self, data: bytearray):
        _i1 = self.to_uint(data[:5], n_bytes=5)
        _i2 = self.to_uint(data[5:], n_bytes=5)
        _ba1 = _i1.to_bytes(4, byteorder="big")
        _ba2 = _i2.to_bytes(4, byteorder="big")
        _s1 = _ba1.decode("utf-8")
        _s2 = _ba2.decode("utf-8")
        self.value = (_s1 + _s2).rstrip("\x00 ")
        return self.formatted

    # Ruida Parameter Types
    def rd_dim(self, data: bytearray):
        self.value = self.to_int(data) / 1000.0
        return self.formatted

    def rd_power(self, data: bytearray):
        self.value = self.to_uint(data) / (0x4000 / 100)
        return self.formatted

    def rd_frequency(self, data: bytearray):
        self.value = self.to_int(data) / 1000
        return self.formatted

    def rd_speed(self, data: bytearray):
        self.value = self.to_int(data) / 1000.0
        return self.formatted

    def rd_time(self, data: bytearray):
        self.value = self.to_int(data) / 1000.0
        return self.formatted

    def rd_rel(self, data: bytearray):
        self.value = self.to_int(data)
        return rdap.RELT.get(data[0], f"REL_UNKNOWN: 0x{data[0]:02X}")

    def rd_axis(self, data: bytearray):
        self.value = self.to_int(data)
        return rdap.AXIS_T.get(data[0], f"UNKNOWN_AXIS: 0x{data[0]:02X}")

    def rd_on_off(self, data: bytearray):
        if data[0]:
            self.value = "ON"
        else:
            self.value = "OFF"
        return self.formatted

    def rd_card_id(self, data: bytearray):
        _id = self.to_uint(data)
        if _id in rdap.CARD_IDS:
            self.value = rdap.CARD_IDS[_id]
        else:
            self.value = f"Unknown: 0x{_id:08X}"
        self.file_checksum = 0
        return self.formatted

    def rd_m_stat(self, data: bytearray):
        _v = self.to_uint(data)
        _s = []
        for _bit, _lbl in rdap.MST:
            if _v & _bit:
                _s.append(_lbl)
        self.value = ", ".join(_s) if _s else f"0x{_v:08X}"
        return self.formatted

    def rd_mt(self, data: bytearray):
        _msb = data[0]
        _lsb = data[1]
        if _msb in rdap.MT:
            if _lsb in rdap.MT[_msb]:
                _lbl = rdap.MT[_msb][_lsb][0]
            else:
                _lbl = rdap.UNKNOWN_LSB
        else:
            _lbl = rdap.UNKNOWN_MSB
        self.value = (_msb << 8) + _lsb
        return self.formatted + ":" + _lbl

    def rd_index(self, data: bytearray):
        _msb = data[0]
        _lsb = data[1]
        if _msb in rdap.IDXT:
            if _lsb in rdap.IDXT[_msb]:
                _lbl = rdap.IDXT[_msb][_lsb][0]
            else:
                _lbl = rdap.UNKNOWN_LSB
        else:
            _lbl = rdap.UNKNOWN_MSB
        self.value = (_msb << 8) + _lsb
        return self.formatted + ":" + _lbl

    def rd_checksum(self, data: bytearray):
        self.value = self.to_uint(data)
        self.checksum = self.value
        return self.formatted

    def rd_tbd(self, data: bytearray):
        self.value = self.to_int(data)
        return self.formatted

    def rd_color(self, data: bytearray):
        """Decode a color value, swapping BGR wire order to RGB for display."""
        raw = self.to_uint(data)
        # Swap R and B bytes (BGR → RGB) in the lower 24 bits
        self.value = ((raw & 0xFF) << 16) | (raw & 0xFF00) | ((raw >> 16) & 0xFF)
        return self.formatted

    # --------------

    def prime(self, spec: tuple, length=None):
        self.out.verbose(f"Priming: {spec}")
        self.format: str = spec[rdap.DFMT]
        self.decoder: str = spec[rdap.DDEC]
        self.rd_type: str = spec[rdap.DTYP]
        self.data: bytearray = bytearray([])
        self.value = None
        self.datum = None
        self.cstring = self.rd_type == "cstring"
        self._rd_decoder = getattr(self, f"rd_{spec[1]}")
        if length is not None:
            self._length = length
        else:
            self._length = rdap.RD_TYPES[self.rd_type][rdap.RDT_BYTES]
        self._remaining = self._length

    @property
    def is_tbd(self):
        return self.rd_type == "tbd"

    def step(self, datum, remaining=None):
        if datum == 0 and self.cstring:
            self.accumulating = False
            self.cstring = False
            return self._rd_decoder(self.data)
        if datum & rdap.CMD_MASK:
            if self.is_tbd:
                self.accumulating = False
                return self._rd_decoder(self.data)
            self.out.protocol(f"datum={datum:02X}: Should not have bit 7 set.")
        if not self.accumulating:
            self.accumulating = True
        self.datum = datum
        self.data.append(datum)
        if remaining is not None:
            self._remaining = remaining
        else:
            self._remaining -= 1
        if self._remaining > 0 or self.is_tbd:
            return None
        else:
            self.accumulating = False
            return self._rd_decoder(self.data)

    def decode_address(self, reply: bytearray) -> int:
        """Extract the memory address from a 9-byte GET_SETTING reply.

        Reply format: [0xDA, 0x01, msb, lsb, d0..d4]
        Returns address using (msb << 8) | lsb, matching rd_mt() convention.
        """
        return (reply[2] << 8) | reply[3]

    def decode_value(self, reply: bytearray) -> int:
        """Extract the unsigned data value from a 9-byte GET_SETTING reply.

        Reply format: [0xDA, 0x01, msb, lsb, d0..d4]
        Extracts bytes 4..8 and decodes as 5-byte unsigned 35-bit integer
        using the same convention as RdDecoder.to_uint().
        """
        return self.to_uint(reply[4:9], n_bytes=5)


class RdEncoder:
    """Encode Python values into Ruida protocol binary data.

    Encoder methods mirror the RdDecoder methods, converting Python values
    back into the 7-bit byte arrays used in Ruida protocol packets.
    All methods are stateless and predictable — same input always produces
    same output.
    """

    # Core encoding utilities — inverses of RdDecoder.to_uint / to_int

    @staticmethod
    def from_uint(value: int, n_bytes: int) -> bytearray:
        """Encode an unsigned integer into n_bytes of 7-bit data (MSB first)."""
        data = bytearray()
        for i in range(n_bytes):
            shift = (n_bytes - 1 - i) * 7
            data.append((value >> shift) & 0x7F)
        return data

    @staticmethod
    def from_int(value: int, n_bytes: int) -> bytearray:
        """Encode a signed integer into n_bytes of 7-bit data.

        Uses the same 2's complement convention as RdDecoder.to_int:
        - First byte: bits 0-5 are data, bit 6 is sign
        - Subsequent bytes: 7 bits of data each
        """
        neg = value < 0
        if neg:
            mask = (1 << (n_bytes * 7 - 1)) - 1
            value = mask + 1 + value  # Two's complement in the data bits
        data = RdEncoder.from_uint(value, n_bytes)
        if neg:
            data[0] |= 0x40
        return data

    # Type-specific encoders

    def encode_int7(self, value: int) -> bytearray:
        return self.from_int(value, 1)

    def encode_uint7(self, value: int) -> bytearray:
        return self.from_uint(value, 1)

    def encode_bool(self, value: bool) -> bytearray:
        return bytearray([0x01 if value else 0x00])

    def encode_int14(self, value: int) -> bytearray:
        return self.from_int(value, 2)

    def encode_uint14(self, value: int) -> bytearray:
        return self.from_uint(value, 2)

    def encode_int35(self, value: int) -> bytearray:
        return self.from_int(value, 5)

    def encode_uint35(self, value: int) -> bytearray:
        return self.from_uint(value, 5)

    @staticmethod
    def encode_color(value: int) -> bytearray:
        """Encode an RGB color value, swapping to BGR byte order for the wire."""
        # Swap R and B bytes (RGB → BGR) in the lower 24 bits
        bgr = ((value & 0xFF) << 16) | (value & 0xFF00) | ((value >> 16) & 0xFF)
        return RdEncoder.from_uint(bgr, 5)

    def encode_cstring(self, value: str) -> bytearray:
        """Encode a string as 7-bit bytes with null terminator."""
        data = bytearray(value.encode("utf-8"))
        data.append(0)
        return data

    def encode_string8(self, value: str) -> bytearray:
        """Encode an 8-character string into 10 bytes of 7-bit data."""
        _padded = value.ljust(8, "\x00")[:8]
        _ba = _padded.encode("utf-8")
        _i1 = int.from_bytes(_ba[:4], byteorder="big")
        _i2 = int.from_bytes(_ba[4:], byteorder="big")
        return self.from_uint(_i1, 5) + self.from_uint(_i2, 5)

    def encode_dim(self, value: float, n_bytes: int = 5) -> bytearray:
        """Encode a dimension (mm) as signed value * 1000.

        Args:
            value: Dimension in mm.
            n_bytes: Number of 7-bit bytes (2 for int14, 5 for int35). Default 5.
        """
        if n_bytes not in (2, 5):
            n_bytes = 5
        return self.from_int(int(round(value * 1000)), n_bytes)

    def encode_power(self, value: float) -> bytearray:
        """Encode a power percentage as uint14 scaled by 0x4000/100."""
        return self.from_uint(int(round(value * 0x4000 / 100)), 2)

    def encode_frequency(self, value: float) -> bytearray:
        """Encode a frequency (KHz) as int35 value * 1000."""
        return self.from_int(int(round(value * 1000)), 5)

    def encode_speed(self, value: float) -> bytearray:
        """Encode a speed (mm/S) as int35 value * 1000."""
        return self.from_int(int(round(value * 1000)), 5)

    def encode_time(self, value: float) -> bytearray:
        """Encode a time (mS) as int35 value * 1000."""
        return self.from_int(int(round(value * 1000)), 5)

    def encode_card_id(self, value: str) -> bytearray:
        """Encode a card ID string (e.g. "RDC6442S") to uint35 bytes.

        Looks up the string in CARD_IDS_BY_NAME to find the numeric ID,
        then encodes as uint35. If the name is not found, encodes 0.
        """
        numeric_id = rdap.CARD_IDS_BY_NAME.get(value, 0)
        return self.from_uint(numeric_id, 5)
