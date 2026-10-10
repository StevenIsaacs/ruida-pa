"""
Ruida UDP communications protocol description tables and definitions.
"""

# Single byte handshaking.
ACK = 0xCC  # Controller received data and is valid.
ERR = 0xCD  # Controller detected a problem with the message.
ENQ = 0xCE  # Keep alive. This should be replied to with a corresponding
# ENQ or ACK.
NAK = 0xCF  # Message checksum mismatch. Resend packet.

CMD_MASK = 0x80  # Only the first byte of a command has the top bit set.
# This, I'm guessing, is the primary reason a 7 bit
# protocol is used.
# This can be used to re-sync or to check the length of
# data associated with a command for validity.
EOF = 0xD7

# This table defines the number of bit and corresponding number of incoming
# data bytes for each basic data type.
# Indexes into the RD_TYPES table.
# e.g. to get the number of bytes needed to decode the value:
#  n_bytes = RD_TYPES[DTYP][RDT_BYTES]
RDT_BITS = 0  # The number of bits in a decoded value.
# These can be used to generate a mask for ignoring
# bits in a larger variable. e.g. A
RDT_BYTES = 1  # The number of data bytes to decode.

RD_TYPES = {
    #   Type            RDT_BITS    RDT_BYTES
    "bool_7": [7, 1],  # Boolean value -- True or False.
    "int_7": [7, 1],
    "uint_7": [7, 1],
    "int_14": [14, 2],
    "uint_14": [14, 2],
    "int_35": [32, 5],  # Top three bits are dropped.
    "uint_35": [32, 5],  # Top three bits are dropped.
    "cstring": [7, 1],  # The values are multiplied by the length
    # of the string.
    "string8": [50, 10],  # An 8 character string.
    "on_off": [1, 1],  # An ON or OFF switch (flag).
    "mt": [14, 2],  # Special handling for a controller memory
    # access (read).
    "index": [14, 2],  # An index into an unknown table.
    "chksum": [32, 5],  # For file checksum calculation.
    "card_id": [32, 5],  # Card ID reply.
}

# Card ID reply to model name lookup table.
CARD_IDS = {
    0x65106510: "RDC6442S",
    0x90109010: "RDC8445S",
}

# Reverse lookup: model name → card ID uint35 value.
CARD_IDS_BY_NAME = {v: k for k, v in CARD_IDS.items()}

# For checking which origin mode.
ORIGIN_HOME = 0x02
LIGHT_ON = 0x01
# Relative option table.
RELT = {
    0x00: "MACHINE",
    0x01: "ANCHOR",
    0x02: "CURRENT",
    0x03: "SET_POINT",
}

# Axis selection for PEN_OFFSET_AXIS / PART_OFFSET_AXIS.
AXIS_T = {
    0: "X",
    1: "Y",
    2: "Z",
    3: "U",
}

# Type format strings.
DIM_FMT = "{:.3f}mm"

# Parameter specifications. NOTE: These are command specific where possible.
# These need to be tuples because the type is used for determining next
# states in the state machine.
# Basic types:
#  Format, decoder, ruida type
# Decoder list indexes.
# e.g. To retrieve the print format for a decoder:
#  format = INT7[DFMT]
# To call the decoding function:
#  r = INT7[DDEC](data)
# To determine the number of bytes decoded:
#  n = INT7[DTYP][RDT_BYTES]
DFMT = 0  # Print format string.
DDEC = 1  # Decoder function to call.
DTYP = 2  # Basic type (used to determine how many bytes to process.)
#               DFMT                DDEC        DTYP
INT7 = ("{}", "int7", "int_7")
UINT7 = ("{}", "uint7", "uint_7")
HEX7 = ("0x{:02X}", "uint7", "uint_7")
BOOL7 = ("{}", "bool", "bool_7")
INT14 = ("{}", "int14", "int_14")
UINT14 = ("{}", "uint14", "uint_14")
HEX14 = ("0x{:04X}", "uint14", "uint_14")
INT35 = ("{}", "int35", "int_35")
UINT35 = ("{}", "uint35", "uint_35")
HEX35 = ("0x{:010X}", "uint35", "uint_35")
CSTRING = ("{}", "cstring", "cstring")
# Parameter types:
FNAME = ("File:{}", "cstring", "cstring")  # File name.
STRING8 = ('String:"{}"', "string8", "string8")  # 8 char string from 2 uint35s.
FNUM = ("FNum:{}", "uint14", "uint_14")
ENAME = ("Elem:{}", "cstring", "cstring")
LAYER = ("Layer:{}", "int7", "int_7")  # or layer.
LASER = ("Laser:{}", "int7", "int_7")  # For dual head lasers.
VALUE = ("{}", "int7", "int_7")
REL = ("Rel:{}", "rel", "int_7")
AXIS = ("Axis:{}", "axis", "int_7")
COLOR = ("Color:#{:06X}", "color", "uint_35")
SETTING = ("Set:{:08X}", "uint35", "uint_35")
ID = ("ID:{}", "uint14", "uint_14")
DIRECTION = ("Dir:{}", "int7", "int_7")  # Table-ize the direction?
DIM = ("POS=" + DIM_FMT, "dim", "int_35")
FARDIM = ("ABS=" + DIM_FMT, "dim", "int_35")
XFARDIM = ("X=" + DIM_FMT, "dim", "int_35")
YFARDIM = ("Y=" + DIM_FMT, "dim", "int_35")
ZFARDIM = ("Z=" + DIM_FMT, "dim", "int_35")
AFARDIM = ("A=" + DIM_FMT, "dim", "int_35")
UFARDIM = ("U=" + DIM_FMT, "dim", "int_35")
OFFSET = ("OFFSET=" + DIM_FMT, "dim", "int_35")
# RELCOORD35, XRELCOORD35, YRELCOORD35 REMOVED — were unused
NEARDIM = ("near=" + DIM_FMT, "dim", "int_14")
XNEARDIM = ("nearX=" + DIM_FMT, "dim", "int_14")
YNEARDIM = ("nearY=" + DIM_FMT, "dim", "int_14")
POWER = ("Power:{:.1f}%", "power", "uint_14")
SPEED = ("Speed:{:.3f}mm/S", "speed", "int_35")
FREQUENCY = ("Freq:{:.3f}KHz", "frequency", "int_35")
COLUMNS = ("Columns={}", "uint14", "uint_14")
ROWS    = ("Rows={}",    "uint14", "uint_14")
XSTEP = ("XStep=" + DIM_FMT, "dim", "int_35")
YSTEP = ("YStep=" + DIM_FMT, "dim", "int_35")
TIME = ("{:.3f}mS", "time", "int_35")
SWITCH = ("State:{}", "on_off", "uint_7")
CARD_ID = ("CardID:{}", "card_id", "uint_35")
M_STAT = ("MStat:{}", "m_stat", "uint_35")
M_FEATURES = ("MFeat:{}", "m_features", "uint_35")

# Machine-axis settings (MT 0x002_..0x005_ per-axis X/Y/Z/U blocks).
PRECISION = ("Prec:{:.3f}mm", "dim", "int_35")
MAX_VELOCITY = ("Vel:{:.3f}mm/S", "speed", "int_35")
START_VELOCITY = ("StartVel:{:.3f}mm/S", "speed", "int_35")
MAX_ACC = ("Acc:{:.3f}mm/S2", "speed", "int_35")
BTN_START_VEL = ("BtnVel:{:.3f}mm/S", "speed", "int_35")
BTN_ACC = ("BtnAcc:{:.3f}mm/S2", "speed", "int_35")
ESTP_ACC = ("EstpAcc:{:.3f}mm/S2", "speed", "int_35")
HOME_OFFSET = ("HomeOffset:" + DIM_FMT, "dim", "int_35")

# A memory access triggers special processing using MT.
MEMORY = ("Addr:{:04X}", "mt", "mt")
# An index into something -- unknown at this time.
INDEX = ("Index:{:04X}", "index", "index")

FILE_SUM = ("Sum:0x{0:010X}", "checksum", "uint_35")

# Use these once the size is known but needs further investigation.
TBDU7 = ("TBDU7:{0:07b}b: 0x{0:02x}: {0}", "uint7", "uint_7")
TBDU14 = ("TBDU14:{0:014b}b: 0x{0:04x}: {0}", "uint14", "uint_14")
TBDU35 = ("TBDU35:{0:035b}b: 0x{0:08x}: {0}", "uint35", "uint_35")
TBD7 = ("TBD7:{0:07b}b: 0x{0:02x}: {0}", "int7", "int_7")
TBD14 = ("TBD14:{0:014b}b: 0x{0:04x}: {0}", "int14", "int_14")
TBD35 = ("TBD35:{0:035b}b: 0x{0:08x}: {0}", "int35", "int_35")

# Reply types.
# Action markers are integers.
REPLY = -1  # An integer to indicate when a reply to a command is expected.
PAUSE = -2  # Can add this to a parameter table to act as a break during decode.
# This is ignored when verbose is not enabled.
# Sometimes bytes appear that don't make sense and look like other
# commands. This is to skip those bytes so they don't confuse the
# parser. This works by disabling the check for commands for N bytes.
# The next entry in the tuple containing SKIP is the number of bytes
# to skip.
SKIP = -3


# Buttons (keys) found on a Ruida control panel.
KT_KEYS = {
    0x01: "X_MINUS",
    0x02: "X_PLUS",
    0x03: "Y_PLUS",
    0x04: "Y_MINUS",
    0x05: "PULSE",
    0x06: "PAUSE",
    0x07: "ESCAPE",
    0x08: "ORIGIN",
    0x09: "STOP",
    0x0A: "Z_PLUS",
    0x0B: "Z_MINUS",
    0x0C: "U_PLUS",
    0x0D: "U_MINUS",
    0x0E: "?",
    0x0F: "TRACE",
    0x10: "?",
    0x11: "SPEED",
    0x12: "LASER_GATE",
}

# Keypad table - port 50207
KT = {
    0x50: ["Press:  ", KT_KEYS],
    0x51: ["Release:", KT_KEYS],
    0x53: {0x00: "INTERFACE_FRAME"},
}

# Memory internal to the controller and readable using the 0xDA command.
# These return 32 bit values. Actual meanings to be defined.
# NOTE: Because only command bytes have the top bit set, the codes
# are limited to 128 for the lower order byte. The upper order byte therefore
# is a multiple of 128.
# Unknown address generic decode.
UNKNOWN_ADDRESS = (
    "TBD:Unknown address",
    TBDU35,
)  # Use when address discovered but data is
# unknown.
# These are used when SETTING at an address.
UNKNOWN_MSB = "MSB TBD"
UNKNOWN_LSB = "LSB TBD"

# Commands which change checksum enable.
KEYPRESS = 0xA7
SETTING = 0xDA
FILE_COMMAND = 0xE5
CHK_DISABLES = (KEYPRESS, SETTING)

SETTING_READ = 0x00
SETTING_WRITE = 0x01

# Bits in a MEM_MACHINE_STATUS value.
MACHINE_STATUS_MOVING = (0x01000000, "Moving")
MACHINE_STATUS_PAUSED = (0x00000002, "Paused")
MACHINE_STATUS_JOB_RUNNING = (0x00000001, "Job Running")

MST = {
    MACHINE_STATUS_MOVING,
    MACHINE_STATUS_PAUSED,
    MACHINE_STATUS_JOB_RUNNING,
}

# Single-bit flags in a MEM_MACHINE_FEATURES value (MST-style).
MACHINE_FEATURE_FOCUS = (0x0001, "Focus")
MACHINE_FEATURE_Z_RETURN_DOCKING = (0x0008, "Z Return to Docking")

MFT = {
    MACHINE_FEATURE_FOCUS,
    MACHINE_FEATURE_Z_RETURN_DOCKING,
}

# Multi-bit value fields in a MEM_MACHINE_FEATURES value:
# (mask, shift, name, {value: label}).
# NOTE: The air-assist mode labels are placeholders pending confirmation of
# the controller's mode values.
MACHINE_FEATURE_AIR_ASSIST = (
    0x0600,
    9,
    "Air Assist Mode",
    {0: "Mode 0", 1: "Mode 1", 2: "Mode 2", 3: "Mode 3"},
)

MFT_FIELDS = (MACHINE_FEATURE_AIR_ASSIST,)

MT = {
    0x00: {
        0x04: ("MEM_IO_ENABLE", TBDU35),
        0x05: ("MEM_G0_VELOCITY", TBDU35),
        0x0B: ("MEM_ENG_FACULA", TBDU35),
        0x0C: ("MEM_HOME_VELOCITY", TBDU35),
        0x0E: ("MEM_ENG_VERT_VELOCITY", TBDU35),
        0x10: ("MEM_SYSTEM_CONTROL_MODE", TBDU35),
        0x11: ("MEM_LASER_PWM_FREQUENCY_1", TBDU35),  # Verified RDC6442S
        0x12: ("MEM_LASER_MIN_POWER_1", TBDU35),  # Verified RDC6442S
        0x13: ("MEM_LASER_MAX_POWER_1", TBDU35),  # Verified RDC6442S
        0x16: ("MEM_LASER_ATTENUATION", TBDU35),
        0x17: ("MEM_LASER_PWM_FREQUENCY_2", TBDU35),
        0x18: ("MEM_LASER_MIN_POWER_2", TBDU35),
        0x19: ("MEM_LASER_MAX_POWER_2", TBDU35),
        0x1A: ("MEM_LASER_STANDBY_FREQUENCY_1", TBDU35),
        0x1B: ("MEM_LASER_STANDBY_PULSE_1", TBDU35),
        0x1C: ("MEM_LASER_STANDBY_FREQUENCY_2", TBDU35),
        0x1D: ("MEM_LASER_STANDBY_PULSE_2", TBDU35),
        0x1E: ("MEM_AUTO_TYPE_SPACE", TBD35),
        0x20: ("MEM_AXIS_CONTROL_PARA_1", TBDU35),
        0x21: ("MEM_PRECISION_X", PRECISION),  # Verified RDC8445S
        0x23: ("MEM_MAX_VELOCITY_X", MAX_VELOCITY),  # Verified RDC8445S
        0x24: ("MEM_START_VELOCITY_X", START_VELOCITY),  # Verified RDC8445S
        0x25: ("MEM_MAX_ACC_X", MAX_ACC),  # Verified RDC8445S
        0x26: ("MEM_BED_SIZE_X", XFARDIM),  # Verified RDC6442S
        0x27: ("MEM_BTN_START_VEL_X", BTN_START_VEL),  # Verified RDC8445S
        0x28: ("MEM_BTN_ACC_X", BTN_ACC),  # Verified RDC8445S
        0x29: ("MEM_ESTP_ACC_X", ESTP_ACC),  # Verified RDC8445S
        0x2A: ("MEM_HOME_OFFSET_X", HOME_OFFSET),  # Verified RDC8445S
        0x2B: ("MEM_AXIS_BACKLASH_1", TBDU35),
        0x30: ("MEM_AXIS_CONTROL_PARA_2", TBDU35),
        0x31: ("MEM_PRECISION_Y", PRECISION),  # Verified RDC8445S
        0x33: ("MEM_MAX_VELOCITY_Y", MAX_VELOCITY),  # Verified RDC8445S
        0x34: ("MEM_START_VELOCITY_Y", START_VELOCITY),  # Verified RDC8445S
        0x35: ("MEM_MAX_ACC_Y", MAX_ACC),  # Verified RDC8445S
        0x36: ("MEM_BED_SIZE_Y", YFARDIM),  # Verified RDC6442S
        0x37: ("MEM_BTN_START_VEL_Y", BTN_START_VEL),  # Verified RDC8445S
        0x38: ("MEM_BTN_ACC_Y", BTN_ACC),  # Verified RDC8445S
        0x39: ("MEM_ESTP_ACC_Y", ESTP_ACC),  # Verified RDC8445S
        0x3A: ("MEM_HOME_OFFSET_Y", HOME_OFFSET),  # Verified RDC8445S
        0x3B: ("MEM_AXIS_BACKLASH_2", TBDU35),
        0x40: ("MEM_AXIS_CONTROL_PARA_3", TBDU35),
        0x41: ("MEM_PRECISION_Z", PRECISION),  # Verified RDC8445S
        0x43: ("MEM_MAX_VELOCITY_Z", MAX_VELOCITY),  # Verified RDC8445S
        0x44: ("MEM_START_VELOCITY_Z", START_VELOCITY),  # Verified RDC8445S
        0x45: ("MEM_MAX_ACC_Z", MAX_ACC),  # Verified RDC8445S
        0x46: ("MEM_TRAVEL_Z", ZFARDIM),  # Verified RDC8445S
        0x47: ("MEM_BTN_START_VEL_Z", BTN_START_VEL),  # Verified RDC8445S
        0x48: ("MEM_BTN_ACC_Z", BTN_ACC),  # Verified RDC8445S
        0x49: ("MEM_ESTP_ACC_Z", ESTP_ACC),  # Verified RDC8445S
        0x4A: ("MEM_HOME_OFFSET_Z", HOME_OFFSET),  # Verified RDC8445S
        0x4B: ("MEM_AXIS_BACKLASH_3", TBDU35),
        0x50: ("MEM_AXIS_CONTROL_PARA_4", TBDU35),
        0x51: ("MEM_PRECISION_U", PRECISION),  # Verified RDC8445S
        0x53: ("MEM_MAX_VELOCITY_U", MAX_VELOCITY),  # Verified RDC8445S
        0x54: ("MEM_START_VELOCITY_U", START_VELOCITY),  # Verified RDC8445S
        0x55: ("MEM_MAX_ACC_U", MAX_ACC),  # Verified RDC8445S
        0x56: ("MEM_TRAVEL_U", UFARDIM),  # Verified RDC8445S
        0x57: ("MEM_BTN_START_VEL_U", BTN_START_VEL),  # Verified RDC8445S
        0x58: ("MEM_BTN_ACC_U", BTN_ACC),  # Verified RDC8445S
        0x59: ("MEM_ESTP_ACC_U", ESTP_ACC),  # Verified RDC8445S
        0x5A: ("MEM_HOME_OFFSET_U", HOME_OFFSET),  # Verified RDC8445S
        0x5B: ("MEM_AXIS_BACKLASH_4", TBDU35),
        0x60: ("MEM_MACHINE_TYPE_(0X1155,_0XAA55)", TBDU35),
        0x63: ("MEM_LASER_MIN_POWER_3", TBDU35),
        0x64: ("MEM_LASER_MAX_POWER_3", TBDU35),
        0x65: ("MEM_LASER_PWM_FREQUENCY_3", TBDU35),
        0x66: ("MEM_LASER_STANDBY_FREQUENCY_3", TBDU35),
        0x67: ("MEM_LASER_STANDBY_PULSE_3", TBDU35),
        0x68: ("MEM_LASER_MIN_POWER_4", TBDU35),
        0x69: ("MEM_LASER_MAX_POWER_4", TBDU35),
        0x6A: ("MEM_LASER_PWM_FREQUENCY_4", TBDU35),
        0x6B: ("MEM_LASER_STANDBY_FREQUENCY_4", TBDU35),
        0x6C: ("MEM_LASER_STANDBY_PULSE_4", TBDU35),
    },
    0x02: {
        0x00: ("MEM_SYSTEM_SETTINGS", TBDU35),
        0x01: ("MEM_TURN_VELOCITY", TBDU35),
        0x02: ("MEM_SYN_ACC", TBDU35),
        0x03: ("MEM_G0_DELAY", TBDU35),
        0x07: ("MEM_FEED_DELAY_AFTER", TBDU35),
        0x09: ("MEM_TURN_ACC", TBDU35),
        0x0A: ("MEM_G0_ACC", TBDU35),
        0x0B: ("MEM_FEED_DELAY_PRIOR", TBDU35),
        0x0C: ("MEM_MANUAL_DIS", TBDU35),
        0x0D: ("MEM_SHUT_DOWN_DELAY", TBDU35),
        0x0E: ("MEM_FOCUS_DEPTH", ZFARDIM),  # Verified RDC8445S
        0x0F: ("MEM_GO_SCALE_BLANK", TBDU35),
        0x1A: ("MEM_ACC_RATIO", TBDU35),
        0x17: ("MEM_ARRAY_FEED_REPAY", TBDU35),
        0x1B: ("MEM_TURN_RATIO", TBDU35),
        0x1C: ("MEM_ACC_G0_RATIO", TBDU35),
        0x1F: ("MEM_ROTATE_PULSE", TBDU35),
        0x21: ("MEM_ROTATE_D", TBDU35),
        0x24: ("MEM_X_MINIMUM_ENG_VELOCITY", TBDU35),
        0x25: ("MEM_X_ENG_ACC", TBDU35),
        0x26: ("MEM_USER_PARA_1", TBDU35),
        0x28: ("MEM_Z_HOME_VELOCITY", TBDU35),
        0x29: ("MEM_Z_WORK_VELOCITY", TBDU35),
        0x2A: ("MEM_Z_G0_VELOCITY", TBDU35),
        0x2B: ("MEM_Z_PEN_UP_POSITION", TBDU35),
        0x2C: ("MEM_U_HOME_VELOCITY", TBDU35),
        0x2D: ("MEM_U_WORK_VELOCITY", TBDU35),
        0x31: ("MEM_MANUAL_FAST_SPEED", TBDU35),
        0x32: ("MEM_MANUAL_SLOW_SPEED", TBDU35),
        0x34: ("MEM_Y_MINIMUM_ENG_VELOCITY", TBDU35),
        0x35: ("MEM_Y_ENG_ACC", TBDU35),
        0x37: ("MEM_ENG_ACC_RATIO", TBDU35),
    },
    0x03: {
        0x00: ("MEM_CARD_LANGUAGE", TBDU35),
        0x01: ("MEM_PC_LOCK_1", TBDU35),
        0x02: ("MEM_PC_LOCK_2", TBDU35),
        0x03: ("MEM_PC_LOCK_3", TBDU35),
        0x04: ("MEM_PC_LOCK_4", TBDU35),
        0x05: ("MEM_PC_LOCK_5", TBDU35),
        0x06: ("MEM_PC_LOCK_6", TBDU35),
        0x07: ("MEM_PC_LOCK_7", TBDU35),
        # Bit/field definitions in MFT (flags) and MFT_FIELDS (value fields).
        0x0F: ("MEM_MACHINE_FEATURES", M_FEATURES),  # Verified RDC8445S
        0x11: ("MEM_TOTAL_LASER_WORK_TIME", TBDU35),
    },
    0x04: {
        0x00: ("MEM_MACHINE_STATUS", M_STAT),  # Verified RDC6442S
        0x01: ("MEM_TOTAL_OPEN_TIME", TBDU35),
        0x02: ("MEM_TOTAL_WORK_TIME", TBDU35),
        0x03: ("MEM_TOTAL_WORK_NUMBER", TBDU35),
        0x05: ("MEM_TOTAL_DOC_NUMBER", TBDU35),
        0x07: ("MEM_UNKNOWN", TBDU35),
        0x08: ("MEM_PRE_WORK_TIME", TBDU35),
        0x21: ("MEM_CURRENT_POSITION_X", XFARDIM),
        0x23: ("MEM_TOTAL_WORK_LENGTH_1", TBDU35),
        0x31: ("MEM_CURRENT_POSITION_Y", YFARDIM),
        0x33: ("MEM_TOTAL_WORK_LENGTH_2", TBDU35),
        0x41: ("MEM_CURRENT_POSITION_Z", ZFARDIM),
        0x43: ("MEM_TOTAL_WORK_LENGTH_3", TBDU35),
        0x51: ("MEM_CURRENT_POSITION_U", UFARDIM),
        0x53: ("MEM_TOTAL_WORK_LENGTH_4", TBDU35),
    },
    0x05: {
        0x7E: ("MEM_CARD_ID", CARD_ID),  # Verified RDC6442S
        0x7F: ("MEM_MAINBOARD_VERSION", CSTRING),  # Verified RDC6442S
    },
    0x06: {
        0x20: UNKNOWN_ADDRESS,
    },
    0x07: {
        0x10: ("MEM_DOCUMENT_TIME", TBDU35),
    },
    0x0B: {
        0x11: ("MEM_CARD_LOCK", TBDU35),
        0x12: ("MEM_UNKNOWN", TBD35),
    },
}

# Index table. This is for replies which appear to index into something but
# exactly what is unknown.
IDXT = {
    0x00: {
        0x00: ("TBD", HEX14, HEX14, HEX14, HEX14, HEX14, HEX14, HEX14, HEX14, HEX14),
    },
}

# Deprecated mnemonic aliases → canonical names. The per-axis X/Y/Z/U memory
# blocks (MT 0x002_..0x005_) were renamed from numbered _1.._4 (and the
# MEM_AXIS_RANGE_3/4 travel names) to axis-suffixed canonical names; the old
# names still resolve through ScriptParser._build_mt_map(). MEM_BED_SIZE_X/Y
# are retained as the canonical X/Y travel names.
MT_MNEMONIC_ALIASES = {
    # X axis (MT 0x002_)
    "MEM_AXIS_PRECISION_1": "MEM_PRECISION_X",
    "MEM_AXIS_MAX_VELOCITY_1": "MEM_MAX_VELOCITY_X",
    "MEM_AXIS_START_VELOCITY_1": "MEM_START_VELOCITY_X",
    "MEM_AXIS_MAX_ACC_1": "MEM_MAX_ACC_X",
    "MEM_AXIS_BTN_START_VEL_1": "MEM_BTN_START_VEL_X",
    "MEM_AXIS_BTN_ACC_1": "MEM_BTN_ACC_X",
    "MEM_AXIS_ESTP_ACC_1": "MEM_ESTP_ACC_X",
    "MEM_AXIS_HOME_OFFSET_1": "MEM_HOME_OFFSET_X",
    # Y axis (MT 0x003_)
    "MEM_AXIS_PRECISION_2": "MEM_PRECISION_Y",
    "MEM_AXIS_MAX_VELOCITY_2": "MEM_MAX_VELOCITY_Y",
    "MEM_AXIS_START_VELOCITY_2": "MEM_START_VELOCITY_Y",
    "MEM_AXIS_MAX_ACC_2": "MEM_MAX_ACC_Y",
    "MEM_AXIS_BTN_START_VEL_2": "MEM_BTN_START_VEL_Y",
    "MEM_AXIS_BTN_ACC_2": "MEM_BTN_ACC_Y",
    "MEM_AXIS_ESTP_ACC_2": "MEM_ESTP_ACC_Y",
    "MEM_AXIS_HOME_OFFSET_2": "MEM_HOME_OFFSET_Y",
    # Z axis (MT 0x004_)
    "MEM_AXIS_PRECISION_3": "MEM_PRECISION_Z",
    "MEM_AXIS_MAX_VELOCITY_3": "MEM_MAX_VELOCITY_Z",
    "MEM_AXIS_START_VELOCITY_3": "MEM_START_VELOCITY_Z",
    "MEM_AXIS_MAX_ACC_3": "MEM_MAX_ACC_Z",
    "MEM_AXIS_RANGE_3": "MEM_TRAVEL_Z",
    "MEM_AXIS_BTN_START_VEL_3": "MEM_BTN_START_VEL_Z",
    "MEM_AXIS_BTN_ACC_3": "MEM_BTN_ACC_Z",
    "MEM_AXIS_ESTP_ACC_3": "MEM_ESTP_ACC_Z",
    "MEM_AXIS_HOME_OFFSET_3": "MEM_HOME_OFFSET_Z",
    # U axis (MT 0x005_)
    "MEM_AXIS_PRECISION_4": "MEM_PRECISION_U",
    "MEM_AXIS_MAX_VELOCITY_4": "MEM_MAX_VELOCITY_U",
    "MEM_AXIS_START_VELOCITY_4": "MEM_START_VELOCITY_U",
    "MEM_AXIS_MAX_ACC_4": "MEM_MAX_ACC_U",
    "MEM_AXIS_RANGE_4": "MEM_TRAVEL_U",
    "MEM_AXIS_BTN_START_VEL_4": "MEM_BTN_START_VEL_U",
    "MEM_AXIS_BTN_ACC_4": "MEM_BTN_ACC_U",
    "MEM_AXIS_ESTP_ACC_4": "MEM_ESTP_ACC_U",
    "MEM_AXIS_HOME_OFFSET_4": "MEM_HOME_OFFSET_U",
}

# Reply table
RT = {
    # TBD Learn during debug.
    SETTING: {
        0x01: ("GET_SETTING", MEMORY),
        0x05: ("TBD", INDEX),
    },
}

# In the command table this indicates a reply is expected and the next entry
# is a reference to the reply in RT.
REPLY = -1

# Command table - port 50200
CT = {
    0x80: {
        0x00: ("MOVE_FAR_X", XFARDIM),  # Verified RDC6442S
        0x01: ("MOVE_FAR_Y", YFARDIM),  # Verified RDC6442S
        0x02: ("MOVE_FAR_U", UFARDIM),
        0x03: ("MOVE_FAR_Z", ZFARDIM),  # Verified RDC6442S
    },
    0x88: ("MOVE_FAR_XY", XFARDIM, YFARDIM),  # Verifired RDC6442S
    0x89: ("MOVE_NEAR_XY", XNEARDIM, YNEARDIM),  # Verified RDC6442S
    0x8A: ("MOVE_NEAR_X", XNEARDIM),  # Verified RDC6442S
    0x8B: ("MOVE_NEAR_Y", YNEARDIM),  # Verified RDC6442S
    0xA0: {
        0x00: ("MOVE_FAR_A", AFARDIM),
        0x08: ("MOVE_FAR_U", UFARDIM),
    },
    0xA7: KT,
    0xA8: ("CUT_FAR_XY", XFARDIM, YFARDIM),  # Verifired RDC6442S
    0xA9: ("CUT_NEAR_XY", XNEARDIM, YNEARDIM),  # Verified RDC6442S
    0xAA: ("CUT_NEAR_X", XNEARDIM),  # Verified RDC6442S
    0xAB: ("CUT_NEAR_Y", YNEARDIM),  # Verified RDC6442S
    0xC0: ("IMD_POWER_2", POWER),
    0xC1: ("END_POWER_2", POWER),
    0xC2: ("IMD_POWER_3", POWER),  # Verified RDC6442S
    0xC3: ("END_POWER_3", POWER),
    0xC4: ("IMD_POWER_4", POWER),
    0xC5: ("END_POWER_4", POWER),
    0xC6: {
        0x01: ("MIN_POWER_1", POWER),
        0x02: ("MAX_POWER_1", POWER),
        0x05: ("MIN_POWER_3", POWER),
        0x06: ("MAX_POWER_3", POWER),
        0x07: ("MIN_POWER_4", POWER),
        0x08: ("MAX_POWER_4", POWER),
        0x10: ("LASER_INTERVAL", TIME),
        0x11: ("ADD_DELAY", TIME),
        0x12: ("LASER_ON_DELAY", TIME),
        0x13: ("LASER_OFF_DELAY", TIME),
        0x15: ("LASER_ON_DELAY2", TIME),
        0x16: ("LASER_OFF_DELAY2", TIME),
        0x21: ("MIN_POWER_2", POWER),
        0x22: ("MAX_POWER_2", POWER),
        0x31: ("LAYER_MIN_POWER_1", LAYER, POWER),  # Verifired RDC6442S
        0x32: ("LAYER_MAX_POWER_1", LAYER, POWER),  # Verifired RDC6442S
        0x35: ("LAYER_MIN_POWER_3", LAYER, POWER),
        0x36: ("LAYER_MAX_POWER_3", LAYER, POWER),
        0x37: ("LAYER_MIN_POWER_4", LAYER, POWER),
        0x38: ("LAYER_MAX_POWER_4", LAYER, POWER),
        0x41: ("LAYER_MIN_POWER_2", LAYER, POWER),
        0x42: ("LAYER_MAX_POWER_2", LAYER, POWER),
        0x50: ("THROUGH_POWER_1", POWER),
        0x51: ("THROUGH_POWER_2", POWER),
        0x55: ("THROUGH_POWER_3", POWER),
        0x56: ("THROUGH_POWER_4", POWER),
        0x60: ("LAYER_FREQUENCY", LASER, LAYER, FREQUENCY),  # Verified RDC6442S
    },
    0xC7: ("IMD_POWER_1", POWER),  # Verified RDC6442S
    0xC8: ("END_POWER_1", POWER),
    0xC9: {
        0x02: ("SPEED_LASER_1", SPEED),
        0x03: ("SPEED_AXIS", SPEED),
        0x04: ("CUT_SPEED_LASER_1", LAYER, SPEED),  # Verifired RDC6442S
        0x05: ("FORCE_ENG_SPEED", SPEED),
        0x06: ("SPEED_AXIS_MOVE", SPEED),
    },
    0xCA: {
        0x01: {
            0x00: "OVERSCAN_OFF",  # Verifired RDC6442S
            0x01: "OVERSCAN_H_BI",  # Verifired RDC6442S
            0x02: "OVERSCAN_H_UNI",  # Verifired RDC6442S
            0x03: "OVERSCAN_V_BI",  # Verifired RDC6442S
            0x04: "OVERSCAN_V_UNI",  # Verifired RDC6442S
            0x05: "OVERSCAN_DIAGONAL",
            0x10: "LASER_DEVICE_0",
            0x11: "LASER_DEVICE_1",
            0x12: "AIR_ASSIST_OFF",  # Verifired RDC6442S
            0x13: "AIR_ASSIST_ON",  # Verifired RDC6442S
            0x14: "DB_HEAD",
            0x30: "EN_LASER_2_OFFSET_0",
            0x31: "EN_LASER_2_OFFSET_1",
            0x55: "OVERSCAN_5",
        },
        0x02: ("SELECT_LAYER", LAYER),  # Verifired RDC6442S
        0x03: ("EN_LASER_TUBE_START", SWITCH),
        0x04: ("X_SIGN_MAP", VALUE),
        0x05: ("DEFAULT_COLOR", COLOR),
        0x06: ("LAYER_COLOR", LAYER, COLOR),  # Verifired RDC6442S
        0x10: ("EN_EX_IO", VALUE),
        0x22: ("LAST_LAYER", LAYER),  # Verifired RDC6442S
        0x30: ("U_FILE_ID", ID),
        0x40: ("ZU_MAP", VALUE),
        0x41: ("LAYER_ATTRIBUTES", LAYER, UINT7),
    },
    ENQ: "ENQ",
    0xD0: {  # This was discovered with LightBurn
        0x29: ("SKIP", SKIP, 2)
    },
    0xD7: "EOF",  # Verifired RDC6442S
    0xD8: {
        0x00: "START_JOB",  # Verifired RDC6442S
        0x01: "STOP_JOB",  # Verified RDC6442S
        0x02: "PAUSE_JOB",  # Verified RDC6442S
        0x03: "RESUME_JOB",  # Verified RDC6442S
        0x10: "REF_POINT_MACHINE",  # Verifired RDC6442S
        0x11: "REF_POINT_ORIGIN",  # Verifired RDC6442S
        0x12: "REF_POINT_CURRENT",  # Verifired RDC6442S
        0x20: "KEYDOWN_X_LEFT",
        0x21: "KEYDOWN_X_RIGHT",
        0x22: "KEYDOWN_Y_TOP",
        0x23: "KEYDOWN_Y_BOTTOM",
        0x24: "KEYDOWN_Z_UP",
        0x25: "KEYDOWN_Z_DOWN",
        0x26: "KEYDOWN_U_FORWARD",
        0x27: "KEYDOWN_U_BACKWARDS",
        0x2A: "HOME_XY",  # Verified RDC6442S
        0x2C: "HOME_Z",  # Verified RDC6442S
        0x2D: "HOME_U",
        0x2E: "FOCUS_Z",  # Verified RDC8445S
        0x30: "KEYUP_LEFT",
        0x31: "KEYUP_RIGHT",
        0x32: "KEYUP_Y_TOP",
        0x33: "KEYUP_Y_BOTTOM",
        0x34: "KEYUP_Z_UP",
        0x35: "KEYUP_Z_DOWN",
        0x36: "KEYUP_U_FORWARD",
        0x37: "KEYUP_U_BACKWARDS",
    },
    0xD9: {
        0x00: ("JOG_X", REL, XFARDIM),  # Verified RDC6442S
        0x01: ("JOG_Y", REL, YFARDIM),  # Verified RDC6442S
        0x02: ("JOG_Z", REL, ZFARDIM),  # Verified RDC6442S
        0x03: ("JOG_U", REL, UFARDIM),
        0x0F: ("JOG_FEED_AXIS", REL),
        0x10: ("JOG_XY", REL, XFARDIM, YFARDIM),  # Verified RDC6442S
        0x30: ("JOG_XYU", REL, XFARDIM, YFARDIM, UFARDIM),
    },
    0xDA: {  # SETTING
        0x00: ("GET_SETTING", MEMORY),  # Verified RDC6442S
        0x01: ("SET_SETTING", MEMORY, TBDU35, TBDU35),  # Verified RDC6442S
        0x05: ("GET_UNKNOWN", INDEX, TBDU35),
    },
    0xE5: {  # FILE
        0x00: ("DOCUMENT_FILE_UPLOAD", FNUM, UINT35, UINT35),
        0x02: "DOCUMENT_FILE_END",
         0x05: ("END_JOB", FILE_SUM),  # Verifired RDC6442S
    },
    0xE6: {
        0x01: "SET_ABSOLUTE",
    },
    0xE7: {
        0x00: "BLOCK_END",  # Verifired RDC6442S
        0x01: ("SET_FILE_NAME", FNAME),
        0x03: ("JOB_TOP_RIGHT", XFARDIM, YFARDIM),  # Verifired RDC6442S
        0x04: ("JOB_COPIES", COLUMNS, ROWS, XSTEP, YSTEP),
        0x05: ("ARRAY_DIRECTION", DIRECTION),
        0x06: ("FEED_REPEAT", UINT35, UINT35),
        0x07: ("JOB_BOTTOM_LEFT", XFARDIM, YFARDIM),  # Verifired RDC6442S
        0x08: ("ARRAY_COPIES", COLUMNS, ROWS, XSTEP, YSTEP),
        0x09: ("FEED_LENGTH", INT35),
        0x0A: ("FEED_INFO", TBD35),
        0x0B: ("ARRAY_EN_MIRROR_CUT", UINT7),
        0x13: ("ARRAY_TOP_RIGHT", XFARDIM, YFARDIM),
        0x17: ("ARRAY_BOTTOM_LEFT", XFARDIM, YFARDIM),
        0x23: ("ARRAY_ADD", XFARDIM, YFARDIM),
        0x24: ("ARRAY_MIRROR", UINT7),
        0x32: ("UNKNOWN_E732", TBDU35, TBDU35),
        0x35: ("BLOCK_X_SIZE", XFARDIM, YFARDIM),
        # ? 0x35: ('BY_TEST: {:08X}', UINT35), # expect 0x11227766?
        0x36: ("SET_FILE_EMPTY", UINT7),
        0x37: ("ARRAY_EVEN_DISTANCE", XSTEP, YSTEP),
        0x38: ("SET_FEED_AUTO_PAUSE", SWITCH),
        0x3A: "UNION_BLOCK_PROPERTY",
        0x50: ("DOCUMENT_TOP_RIGHT", XFARDIM, YFARDIM),
        0x51: ("DOCUMENT_BOTTOM_LEFT", XFARDIM, YFARDIM),
        0x52: ("LAYER_TOP_RIGHT", LAYER, XFARDIM, YFARDIM),  # Verifired RDC6442S
        0x53: ("LAYER_BOTTOM_LEFT", LAYER, XFARDIM, YFARDIM),  # Verifired RDC6442S
        0x54: ("PEN_OFFSET_AXIS", AXIS, OFFSET),
        0x55: ("LAYER_OFFSET_AXIS", AXIS, OFFSET),
        0x60: ("SET_CURRENT_ELEMENT_INDEX", UINT7),
        0x61: ("LAYER_EX_TOP_RIGHT", LAYER, XFARDIM, YFARDIM),
        0x62: ("LAYER_EX_BOTTOM_LEFT", LAYER, XFARDIM, YFARDIM),
    },
    0xE8: {
        0x00: ("DELETE_DOCUMENT", UINT35, UINT35),
        0x01: ("DOCUMENT_NUMBER", UINT14),
        0x02: "FILE_TRANSFER",
        0x03: ("SELECT_DOCUMENT", UINT7),
        0x04: "CALCULATE_DOCUMENT_TIME",
    },
    0xEA: ("ARRAY_START", UINT7),
    0xEB: "ARRAY_END",  # Verifired RDC6442S
    0xF0: "REF_POINT_SET",
    0xF1: {
        0x00: ("ELEMENT_MAX_INDEX", UINT7),
        0x01: ("ELEMENT_NAME_MAX_INDEX", UINT7),
        0x02: ("ENABLE_BLOCK_CUTTING", SWITCH),
        0x03: ("DISPLAY_OFFSET", XFARDIM, YFARDIM),
        0x04: ("FEED_AUTO_CALC", UINT7),
    },
    0xF2: {
        0x00: ("ELEMENT_INDEX", UINT7),
        0x01: ("ELEMENT_NAME_INDEX", UINT7),
        0x02: ("ELEMENT_NAME", STRING8),
        0x03: ("ELEMENT_ARRAY_TOP_RIGHT", XFARDIM, YFARDIM),
        0x04: ("ELEMENT_ARRAY_BOTTOM_LEFT", XFARDIM, YFARDIM),
        0x05: ("ELEMENT_COPIES", COLUMNS, ROWS, XSTEP, YSTEP),
        0x06: ("ELEMENT_ARRAY_ADD", XFARDIM, YFARDIM),
        0x07: ("ELEMENT_ARRAY_MIRROR", UINT7),
    },
}
