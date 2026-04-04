"""
AIVDM/AIVDO NMEA Decoder — stdlib only

Decodes AIS messages from 6-bit ASCII armored NMEA sentences.
Supports message types 1-3 (position), 5 (static/voyage),
18 (Class B position), and 24 (Class B static).

Reference: ITU-R M.1371-5, gpsd AIVDM documentation.

6-bit encoding is similar to how audio uses mu-law companding —
a compact, standardized way to pack data into a constrained channel.
NMEA sentences are the "transport frames" carrying the AIS payload,
like how AES/EBU frames carry audio samples.
"""

import time
import logging

log = logging.getLogger("ais-decoder")

# ─── Bit manipulation ───────────────────────────────────

def dearmor(payload: str, fill_bits: int) -> list[int]:
    """
    Convert 6-bit ASCII armored payload to a flat list of bits.

    Each character maps to a 6-bit value:
      - subtract 48
      - if result > 40, subtract 8 more
    Then expand each value to 6 bits (MSB first).

    This is conceptually like unpacking packed audio samples
    from a byte stream — fixed-width fields, MSB-first.
    """
    bits = []
    for ch in payload:
        val = ord(ch) - 48
        if val > 40:
            val -= 8
        for i in range(5, -1, -1):
            bits.append((val >> i) & 1)

    # Trim fill bits from the last character
    if fill_bits > 0:
        bits = bits[:-fill_bits]

    return bits


def get_uint(bits: list[int], offset: int, width: int) -> int:
    """Extract an unsigned integer from the bit array."""
    val = 0
    for i in range(width):
        val = (val << 1) | bits[offset + i]
    return val


def get_int(bits: list[int], offset: int, width: int) -> int:
    """Extract a signed (two's complement) integer from the bit array."""
    val = get_uint(bits, offset, width)
    if val >= (1 << (width - 1)):
        val -= (1 << width)
    return val


# ITU-R M.1371 6-bit text: maps 0-63 to @A-Z[\]^_ !"#$%&'()*+,-./0-9:;<=>?
_AIS_CHARS = "@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_ !\"#$%&'()*+,-./0123456789:;<=>?"


def get_text(bits: list[int], offset: int, width: int) -> str:
    """Extract 6-bit encoded text. Returns stripped string."""
    chars = []
    for i in range(0, width, 6):
        idx = get_uint(bits, offset + i, 6)
        chars.append(_AIS_CHARS[idx])
    return "".join(chars).rstrip("@ ")


# ─── Navigation status lookup ───────────────────────────

NAV_STATUS = {
    0: "under way using engine",
    1: "at anchor",
    2: "not under command",
    3: "restricted manoeuvrability",
    4: "constrained by draught",
    5: "moored",
    6: "aground",
    7: "engaged in fishing",
    8: "under way sailing",
    14: "AIS-SART",
    15: "not defined",
}

# ─── Message type decoders ──────────────────────────────

def decode_msg_1_2_3(bits: list[int]) -> dict:
    """
    Common Navigation Block — position reports.
    168 bits. The workhorse of AIS — like Mode S squitters in ADS-B.
    """
    if len(bits) < 168:
        return {}

    mmsi = get_uint(bits, 8, 30)
    nav_status = get_uint(bits, 38, 4)
    rot_raw = get_int(bits, 42, 8)  # rate of turn (not decoded further)
    sog_raw = get_uint(bits, 50, 10)
    accuracy = get_uint(bits, 60, 1)
    lon_raw = get_int(bits, 61, 28)
    lat_raw = get_int(bits, 89, 27)
    cog_raw = get_uint(bits, 116, 12)
    heading = get_uint(bits, 128, 9)
    ts_second = get_uint(bits, 137, 6)

    result = {
        "mmsi": mmsi,
        "msg_type": get_uint(bits, 0, 6),
        "nav_status": nav_status if nav_status != 15 else None,
    }

    # Speed over ground: 1/10 knot, 1023 = not available
    if sog_raw < 1023:
        result["speed"] = sog_raw / 10.0

    # Longitude: 1/10000 min, 181 degrees = not available
    lon = lon_raw / 600000.0
    if abs(lon) <= 180.0:
        result["lon"] = round(lon, 6)

    # Latitude: 1/10000 min, 91 degrees = not available
    lat = lat_raw / 600000.0
    if abs(lat) <= 90.0:
        result["lat"] = round(lat, 6)

    # Course over ground: 1/10 degree, 3600 = not available
    if cog_raw < 3600:
        result["course"] = cog_raw / 10.0

    # True heading: 511 = not available
    if heading < 511:
        result["heading"] = heading

    return result


def decode_msg_5(bits: list[int]) -> dict:
    """
    Static and Voyage Related Data — vessel identity.
    424 bits, spans 2 NMEA sentences. This is the vessel's "business card."
    """
    if len(bits) < 424:
        return {}

    mmsi = get_uint(bits, 8, 30)
    imo = get_uint(bits, 40, 30)
    callsign = get_text(bits, 70, 42)
    ship_name = get_text(bits, 112, 120)
    ship_type = get_uint(bits, 232, 8)

    dim_bow = get_uint(bits, 240, 9)
    dim_stern = get_uint(bits, 249, 9)
    dim_port = get_uint(bits, 258, 6)
    dim_starboard = get_uint(bits, 264, 6)

    draught_raw = get_uint(bits, 294, 8)
    destination = get_text(bits, 302, 120)

    result = {
        "mmsi": mmsi,
        "msg_type": 5,
    }

    if imo > 0:
        result["imo"] = imo
    if callsign:
        result["callsign"] = callsign
    if ship_name:
        result["ship_name"] = ship_name
    if ship_type > 0:
        result["ship_type"] = ship_type
    if dim_bow > 0:
        result["dim_bow"] = dim_bow
    if dim_stern > 0:
        result["dim_stern"] = dim_stern
    if dim_port > 0:
        result["dim_port"] = dim_port
    if dim_starboard > 0:
        result["dim_starboard"] = dim_starboard
    if destination:
        result["destination"] = destination

    return result


def decode_msg_18(bits: list[int]) -> dict:
    """
    Standard Class B CS Position Report — leisure/small vessels.
    168 bits. Same idea as type 1-3 but for Class B transponders
    (cheaper units common on sailboats, fishing boats in the Saronic).
    """
    if len(bits) < 168:
        return {}

    mmsi = get_uint(bits, 8, 30)
    sog_raw = get_uint(bits, 46, 10)
    accuracy = get_uint(bits, 56, 1)
    lon_raw = get_int(bits, 57, 28)
    lat_raw = get_int(bits, 85, 27)
    cog_raw = get_uint(bits, 112, 12)
    heading = get_uint(bits, 124, 9)

    result = {
        "mmsi": mmsi,
        "msg_type": 18,
    }

    if sog_raw < 1023:
        result["speed"] = sog_raw / 10.0

    lon = lon_raw / 600000.0
    if abs(lon) <= 180.0:
        result["lon"] = round(lon, 6)

    lat = lat_raw / 600000.0
    if abs(lat) <= 90.0:
        result["lat"] = round(lat, 6)

    if cog_raw < 3600:
        result["course"] = cog_raw / 10.0

    if heading < 511:
        result["heading"] = heading

    return result


def decode_msg_24(bits: list[int]) -> dict | None:
    """
    Class B CS Static Data Report — identity for Class B vessels.
    168 bits. Comes in two parts:
      Part A (part_num=0): vessel name
      Part B (part_num=1): ship type, callsign, dimensions
    """
    if len(bits) < 160:
        return None

    mmsi = get_uint(bits, 8, 30)
    part_num = get_uint(bits, 38, 2)

    result = {
        "mmsi": mmsi,
        "msg_type": 24,
    }

    if part_num == 0:
        # Part A: vessel name
        ship_name = get_text(bits, 40, 120)
        if ship_name:
            result["ship_name"] = ship_name
    elif part_num == 1:
        # Part B: type, callsign, dimensions
        ship_type = get_uint(bits, 40, 8)
        callsign = get_text(bits, 90, 42)
        dim_bow = get_uint(bits, 132, 9)
        dim_stern = get_uint(bits, 141, 9)
        dim_port = get_uint(bits, 150, 6)
        dim_starboard = get_uint(bits, 156, 6)

        if ship_type > 0:
            result["ship_type"] = ship_type
        if callsign:
            result["callsign"] = callsign
        if dim_bow > 0:
            result["dim_bow"] = dim_bow
        if dim_stern > 0:
            result["dim_stern"] = dim_stern
        if dim_port > 0:
            result["dim_port"] = dim_port
        if dim_starboard > 0:
            result["dim_starboard"] = dim_starboard
    else:
        return None

    return result


# ─── NMEA sentence parsing & multi-sentence assembly ────

# Decoder dispatch table
_DECODERS = {
    1: decode_msg_1_2_3,
    2: decode_msg_1_2_3,
    3: decode_msg_1_2_3,
    5: decode_msg_5,
    18: decode_msg_18,
    24: decode_msg_24,
}


class NMEAAssembler:
    """
    Handles multi-sentence AIVDM messages (e.g., type 5 spans 2 sentences).

    Like reassembling TCP segments or multipart MIME — buffer fragments,
    emit the complete payload when all parts arrive.
    """

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        # Key: (fragment_count, message_id, channel) -> (accumulated_payload, timestamp)
        self._pending: dict[tuple, tuple[str, float]] = {}

    def process(self, sentence: str) -> str | None:
        """
        Feed a raw NMEA sentence. Returns the complete payload string
        when all fragments are assembled, or None if still waiting.

        Single-sentence messages return immediately.
        """
        # Strip and validate
        sentence = sentence.strip()
        if not sentence.startswith("!"):
            return None

        # Remove checksum
        if "*" in sentence:
            sentence = sentence[:sentence.index("*")]

        parts = sentence.split(",")
        if len(parts) < 7:
            return None

        # !AIVDM,frag_count,frag_num,msg_id,channel,payload,fill_bits
        try:
            frag_count = int(parts[1])
            frag_num = int(parts[2])
        except (ValueError, IndexError):
            return None

        msg_id = parts[3]  # empty for single-sentence
        channel = parts[4]
        payload = parts[5]
        try:
            fill_bits = int(parts[6]) if parts[6] else 0
        except ValueError:
            fill_bits = 0

        # Single-sentence message — return immediately
        if frag_count == 1:
            return payload + ":" + str(fill_bits)

        # Multi-sentence: buffer and reassemble
        key = (frag_count, msg_id, channel)
        now = time.monotonic()

        # Purge stale fragments
        stale = [k for k, (_, ts) in self._pending.items() if now - ts > self.timeout]
        for k in stale:
            del self._pending[k]

        if frag_num == 1:
            # First fragment — store payload
            self._pending[key] = (payload, now)
            return None
        else:
            # Subsequent fragment — concatenate
            if key not in self._pending:
                return None  # orphan fragment
            accumulated, _ = self._pending[key]
            accumulated += payload

            if frag_num == frag_count:
                # All fragments received — emit
                del self._pending[key]
                return accumulated + ":" + str(fill_bits)
            else:
                # Still waiting for more
                self._pending[key] = (accumulated, now)
                return None


def decode_nmea(sentence: str, assembler: NMEAAssembler) -> dict | None:
    """
    Top-level entry point: feed a raw NMEA sentence, get a decoded dict
    (or None if the message is incomplete, unsupported, or malformed).
    """
    assembled = assembler.process(sentence)
    if assembled is None:
        return None

    # Split payload:fill_bits
    payload, _, fill_str = assembled.rpartition(":")
    try:
        fill_bits = int(fill_str)
    except ValueError:
        fill_bits = 0

    if not payload:
        return None

    # Dearmor
    try:
        bits = dearmor(payload, fill_bits)
    except Exception:
        return None

    if len(bits) < 6:
        return None

    # Get message type from first 6 bits
    msg_type = get_uint(bits, 0, 6)

    decoder = _DECODERS.get(msg_type)
    if decoder is None:
        return None  # unsupported message type

    try:
        return decoder(bits)
    except (IndexError, ValueError) as e:
        log.debug(f"Failed to decode type {msg_type}: {e}")
        return None
