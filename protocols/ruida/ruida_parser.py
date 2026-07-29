"""A state machine for parsing an input stream byte by byte.

This state machine must be driven by repeatedly calling "step" with a single
byte and whether the current byte is part of a reply or not.

NOTE: This does not verify the host/controller packet handshake.
"""

import protocols.ruida.rpa_plotter as rpa_plotter
import protocols.ruida.ruida_protocol as rdap
from rpalib.rpa_emitter import RpaEmitter
from rpalib.ruida_transcoder import RdDecoder


class RdParser:
    """This is a state machine for parsing and decoding an Ruida protocol
    input stream.

    The parser is driven by repeated calls to "step" with a single byte.
    When a decode is complete step returns decoded data.

    NOTE: These tables were constructed using the information provided by
    tatarize here: https://edutechwiki.unige.ch/en/Ruida
    The command labels are defined in rdjob.py.

    Attributes:
        datum           The data byte being processed.
        remaining       The number of bytes remaining in the current packet.
                        0 indicate the end of the packet.
        state           The name of the current state.
        last            The data byte processed in the previous step.
        last_is_reply   When True the last byte was from a reply.
        data            The data accumulated since the last decoded.
        command         The current command being parsed.
        command_number  The number of commands processed or the number of the
                        command being processed.
        sub_command     The current sub-command being parsed.
        parameters      The list of decoded parameter values.
        reply_command   The command byte from a reply from the controller.
        reply_sub_command
                        The sub-command byte from a reply from the controller.
        command_bytes   The accumulated command bytes -- including sub-command
                        and parameters.
        host_bytes      The bytes from the host since the last parser output.
                        These are displayed when verbose is enabled.
        controller_byres The bytes from the controller since the last parser
                        output. These are displayed when verbose is enabled.
        param_bytes     The accumulated parameter bytes for the current parameter.
        reply_bytes     The accumulated reply bytes.
        decoded         The decoded command string. This string grows as a
                        command is parsed and decoded.
        file_checksum   Total checksum of bytes from the host (NOT replies).
        checksum        When True bytes from the host will be included in the
                        checksum.
        verbose         The method to call when emitting verbose messages.
    """

    def __init__(self, output: RpaEmitter, title: str):
        """Initialize the parsing state machine.

        Parameters:
            output      The output stream for emitting verbose messages.
            label       The label to associate with the parsing and plot.
        """
        self.out = output
        self.title = title
        self.state = None
        self.datum = None
        self.remaining = None
        self.last = None
        self.is_reply = False
        self.last_is_reply = False
        self.data = bytearray([])
        self.command = None
        self.command_number = 0
        self.last_command = None
        self.sub_command = None
        self.last_sub_command = None
        self.reply_command = None
        self.reply_sub_command = None
        self.mt_values = []
        self.mt_address_msb = None
        self.mt_address_lsb = None
        self.param_list = None
        self.which_param = None
        self.cmd_values = []
        self.command_bytes = []
        self.param_bytes = []
        self.host_bytes: bytearray = bytearray([])
        self.controller_bytes: bytearray = bytearray([])
        self.decoder = RdDecoder(output)
        self.label = ""
        self.decoded = ""
        self.on_command = None  # Optional callback for script generation
        self.checksum_enabled = False

        self._ct = rdap.CT  # The command table to use for parsing. This changes
        # for sub-commands and for expected replies.
        self._it = None  # For indexing into memory or index table.
        self._stepper = None  # For commands.
        self._sub_stepper = None  # For parameters.
        self._transition = None
        self._enter_state("sync")  # Setup the sync state.
        self._transition()
        self._skip = 0
        self.plot = rpa_plotter.RpaPlotter(self.out, self.title)

    def _format_decoded(self, message: str, param=None):
        """Accumulate decoded messages one by one.

        The sections of messages are accumulated by appending strings to
        the decoded string."""
        if param is not None:
            self.decoded += message.format(param)
        else:
            self.decoded += message

    # +++++++++++++++ State Machine
    # Internal states. Every state is required to have two handlers identified
    # by the following prefixes:
    #  _tr_... State transition handler.
    #           A transition handler prepares for entry to the next state. It
    #           sets the next state. All transitions receive the current datum
    #           because a transition can make decisions based upon its value.
    #  _st_... State handler.
    #           A state handler returns the decode message once a message has
    #           been decoded. Otherwise, None is returned.
    #           A state handler calls transition methods when a transition to
    #           another state is required.
    #  _h_...   A state or transition helper. This handles logic that is common
    #           across commands or transitions.
    # +++++++++++++++ Helpers

    def _enter_state(self, state: str):
        """Enter a state.

        This uses the state name to derive the names of the state transition
        and stepper methods to set the state reference and call its corresponding
        transition method.


        Parameter:
            state   The name of the state.
        """
        if self.state is not None:
            self.out.verbose(f"Exiting state: {self.state}")
        self.out.verbose(f"Entering state: {state}")
        self._transition = getattr(self, f"_tr_{state}")
        self._stepper = getattr(self, f"_st_{state}")
        self.state = state

    def _next_state(self):
        """Transition to the next state."""
        if self._transition is not None:
            self._transition()
            self._transition = None

    def _forward_to_state(self, state: str):
        """Enter the state and pass the current datum to the state for
        immediate parsing.

        This calls the state after entering it and returns the result of
        parsing the current datum.
        """
        self.out.verbose(f"Forwarding 0x{self.datum:02X} to state {state}")
        self._enter_state(state)
        return self._stepper(self.datum)

    # ++++ Helpers
    def _h_is_command(self, datum):
        """Return True if the datum is a command byte."""
        return (datum & rdap.CMD_MASK) == rdap.CMD_MASK

    def _h_is_known_command(self, datum):
        """Check the datum to see if it is a member of the current command
        table. This works for normal commands and sub-commands."""
        return datum in self._ct

    def _h_prepare_for_command(self):
        self.data = []
        self.last_command = self.command
        self.command = None
        self.command_number += 1
        self.out.set_cmd_n(self.command_number)
        self.out.info("Next command...")
        self.last_sub_command = self.sub_command
        self.sub_command = None
        self.cmd_values = []
        self.command_bytes = []
        self.param_bytes = []
        self._ct = rdap.CT
        self._disable_checksum()

    def _h_check_for_reply(self):
        _param = self.param_list[self.which_param]
        _t = type(_param)
        if _t is tuple:
            # A reply is expected to be atomic. Therefore all remaining
            # bytes in the reply packet are captured for decode.
            self.decoder.prime(self.param_list[self.which_param])
            self.out.verbose(f"Decoding parameter {self.which_param}.")
        elif _t is int:
            # Action marker.
            if _param == rdap.REPLY:
                # Advance to the next parameter -- skip the rdap.REPLY marker.
                _next = self.which_param + 1
                if _next > len(self.param_list):
                    self.out.protocol("No reply type following reply marker.")
                    self._enter_state("sync")
                else:
                    self.which_param = _next
                    if self.remaining == 0:
                        self._enter_state("expect_reply")
                    else:
                        self._enter_state("expect_command")
            else:
                self.out.protocol("Invalid action marker in parameter list.")
        else:
            self.out.protocol("Unexpected type in parameter list.")

    def _h_show_parse_data(self):
        self.out.write(f"Spec:{self.param_list}")
        self.out.write(f"-->:{self.host_bytes.hex()}")
        self.out.write(f"<--:{self.controller_bytes.hex()}")

    def _h_data_error(self, message):
        """Display and error with incoming data and bytes leading up to
        the error."""
        self._h_show_parse_data()
        self.out.error(message)

    def _h_protocol_error(self, message):
        """Display and error with incoming data and bytes leading up to
        the error."""
        self._h_show_parse_data()
        self.out.protocol(message)

    def _h_end_decode(self):
        """Terminate a decode when a problem occurs while decoding data.

        This completes a decode and transitions to the sync state. The
        decode result and the result from the transition are returned."""
        if self.decoder.is_tbd:
            _rd = self.decoder.step(rdap.EOD)
            if _rd is not None:
                _r = self.decoded + ":" + _rd
        else:
            _rd = None
        _rs = self._forward_to_state("sync")
        if _rd is None and _rs is None:
            return None
        if _rd is None:
            _r = ""
        if _rs is not None:
            _r += "\n" + _rs
        return _r

    def _enable_checksum(self):
        """When enabled every byte from the host is added to an overall
        checksum.

        This is used to start a file checksum.

        Only data from the host is included in the checksum.
        """
        self.checksum_enabled = True
        self.out.verbose("Checksum: ENABLED")

    def _disable_checksum(self):
        """Disable checksum calculation.

        This is used to disable checksum calculation.
        """
        self.checksum_enabled = False
        self.out.verbose("Checksum: disabled")

    def _reset_checksum(self):
        """Disable the checksum and reset the overall checksum to 0."""
        self.checksum_enabled = True
        self.decoder.file_checksum = 0
        self.decoder.checksum = 0

    def _add_to_checksum(self, chk):
        """Add the datum to the checksum when enabled."""
        if self.checksum_enabled:
            self.out.verbose(f"Adding {chk} to checksum.")
            self.decoder.file_checksum += chk

    def _backout_checksum(self, data):
        if type(data) is list:
            for _d in data:
                self.decoder.file_checksum -= _d
        else:
            self.decoder.file_checksum -= data
        self.out.verbose(f"Backed out: {data}")

    def _verify_checksum(self):
        """Returns True if the checksums match."""
        return self.decoder.file_checksum == self.decoder.checksum

    # ---- Helpers

    # ++++ MEMORY reply
    # ++++
    def _st_mt_decode_reply(self, datum):
        if self.is_reply:
            _r = self.decoder.step(datum)
            if _r is not None:
                self.mt_values.append(self.decoder.value)
                # Parameter has been decoded.
                self.out.verbose(f"Decoded reply parameter {self.which_param}={_r}.")
                if self.which_param == 1:
                    self.decoded += _r
                else:
                    self.decoded += " " + _r
                # Advance to the next parameter.
                _next = self.which_param + 1
                if _next >= len(self.param_list):
                    self.plot.mt_update(
                        self.mt_address_msb, self.mt_address_lsb, self.mt_values
                    )
                    self.out.verbose("Reply decoded.")
                    if self.remaining == 0:
                        self._enter_state("expect_command")
                    else:
                        self._enter_state("mt_command")
                    return self.decoded
                else:
                    self.which_param = _next
                    self.decoder.prime(self.param_list[self.which_param])
        else:
            self.out.error("Packet from host when decoding reply data.")
            return self._h_end_decode()
        return None

    def _tr_mt_decode_reply(self):
        if self.mt_address_msb not in self._it:
            # Setup a generic decode for an unknown address.
            _reply = rdap.UNKNOWN_ADDRESS
        else:
            _msb = self.mt_address_msb
            _lsb = self.mt_address_lsb
            self.out.verbose(f"Memory reference: {_msb:02X}{_lsb:02X}")
            if _lsb not in self._it[_msb]:
                # Setup a generic decode for an unknown address.
                _reply = rdap.UNKNOWN_ADDRESS
            else:
                _reply = self._it[_msb][_lsb]
        self.param_list = _reply
        # Build the first line: "GET_SETTING <mnemonic>" or "GET_SETTING 0x<addr>"
        _addr_val = (self.mt_address_msb << 8) + self.mt_address_lsb
        _addr_hex = f"0x{_addr_val:04X}"
        if _reply is rdap.UNKNOWN_ADDRESS:
            self.decoded += f" {_addr_hex}"
        else:
            self.decoded += f" {_reply[0]}"
        # Start the comment line with address format
        self.decoded += f"\n# Addr:{_addr_hex}="
        self.which_param = 1
        if "tbd" in _reply[1]:
            self.decoder.prime(_reply[1], length=self.remaining)
        else:
            self.decoder.prime(_reply[1])
        self.mt_values = []

    # ----

    # ++++
    def _st_mt_address_lsb(self, datum):
        if self.is_reply:
            if self.mt_address_msb not in self._it:
                # Setup a generic decode for an unknown address.
                pass
            else:
                if datum not in self._it[self.mt_address_msb]:
                    self.out.protocol(f"Unknown MT address LSB (0x{datum:02X}).")
            self.mt_address_lsb = datum
            self._enter_state("mt_decode_reply")
        else:
            self.out.error("Packet from host when expecting reply memory address.")
            return self._forward_to_state("sync")

    def _tr_mt_address_lsb(self):
        self.mt_address_lsb = None

    # ----
    # ++++
    def _st_mt_address_msb(self, datum):
        if self.is_reply:
            if datum not in self._it:
                self.out.protocol(f"Unknown MT address MSB (0x{datum:02X}.)")
            self.mt_address_msb = datum
            self._enter_state("mt_address_lsb")
        else:
            self.out.error("Packet from host when expecting reply memory address.")
            return self._forward_to_state("sync")

    def _tr_mt_address_msb(self):
        self.mt_address_msb = None

    # ----
    # ++++
    def _st_mt_sub_command(self, datum):
        if self.is_reply:
            # A reply to a memory access always has a sub-command.
            if self._h_is_known_command(datum):
                if type(self._ct[datum]) is tuple:
                    self.reply_sub_command = datum
                    self.decoded = self.label = self._ct[datum][0]
                    self._enter_state("mt_address_msb")
                else:
                    self.out.protocol("A reply data type should be a tuple.")
                    self._enter_state("sync")
            else:
                self.out.error(
                    f"Datum (0x{datum:02X} is not a known reply sub_command)"
                )
                self._enter_state("sync")
        else:
            self.out.error("Packet from host when expecting reply sub_command.")
            return self._forward_to_state("sync")

    def _tr_mt_sub_command(self):
        self._ct = rdap.RT[self.reply_command]

    # ----

    # ++++
    def _st_mt_command(self, datum):
        if self.is_reply:
            if self._h_is_command(datum):
                # A reply to a memory access always has a sub-command.
                if self._h_is_known_command(datum):
                    if type(self._ct[datum]) is dict:
                        self.reply_command = datum
                        self._enter_state("mt_sub_command")
                    else:
                        self.out.protocol(
                            "A reply sub-command type should be a dictionary."
                        )
                        self._enter_state("sync")
                else:
                    self.out.error(
                        f"Datum (0x{datum:02X} is not a known reply command)"
                    )
                    self._enter_state("sync")
            else:
                self.out.error(f"Datum (0x{datum:02X} is not a reply command byte.)")
                self._enter_state("sync")
        else:
            self.out.error("Current packet is NOT a reply packet.")
            return self._forward_to_state("sync")

    def _tr_mt_command(self):
        """Setup to parse a reply to a memory read command.

        This state is triggered when the command parameter list contains
        a MEMORY spec and the memory command has been decoded."""
        if self.command == 0xDA:  # Reading from controller.
            self.reply_command = None
            self._ct = rdap.RT
            self._it = rdap.MT
        else:
            self.out.protocol(
                f"Memory reference with wrong command: 0x{self.command:02X}"
            )

    # An index is handled identically to an mt.
    def _st_index_command(self, datum):
        return self._st_mt_command(datum)

    def _tr_index_command(self):
        """Setup to parse a reply to a indexed read command.

        This state is triggered when the command parameter list contains
        a INDEX spec and the index command has been decoded."""
        if self.command == 0xDA:  # Reading from controller.
            self.reply_command = None
            self._ct = rdap.RT
            self._it = rdap.IDXT
        else:
            self.out.protocol(
                f"Indexed reference with wrong command: 0x{self.command:02X}"
            )

    # ----

    # ---- MEMORY reply states
    # ++++
    def _st_expect_reply(self, datum):
        """Expect and decode reply data from the controller.

        Reply packets are atomic responses meaning: one command, one reply.

        The reply data is appended to the parameter list."""
        if not self.is_reply:
            # If the reply type is TBD then reached the end of the reply.
            if self.decoder.is_tbd:
                _r = self.decoder(rdap.CMD_MASK)
                if _r is None:
                    _r = ""
            else:
                self.out.error("Packet from host when expecting reply.")
            return _r + self._forward_to_state("sync")
        else:
            if self._h_is_command(datum):
                self._h_data_error(
                    f"Datum 0x{datum:02X} is a command -- expected data."
                )
                return self._forward_to_state("sync")
            else:
                _r = self.decoder.step(datum, self.remaining)
                if _r is not None:
                    # Parameter has been decoded.
                    self.out.verbose("Decoded reply.")
                    self.decoded += "Reply=" + _r
                    return self.decoded
        return None

    def _tr_expect_reply(self):
        if self.decoded is None:
            self.decoded = ""
        else:
            self.decoded += "\n"
        self.decoder.prime(self.param_list[self.which_param])

    # ----

    # ++++
    def _st_decode_parameters(self, datum):
        if self.is_reply:
            self.out.error("Reply packet when expecting parameters.")
            self._forward_to_state("mt_command")
        else:
            if self._h_is_command(datum):
                # This can either be a problem with the incoming data or
                # the definition in the protocol table.
                if not self.decoder.is_tbd:
                    self._h_data_error(
                        f"Datum 0x{datum:02X} is a command -- expected data."
                    )
                return self._h_end_decode()
            else:
                _r = self.decoder.step(datum)
                if _r is not None:
                    # Parameter has been decoded.
                    self.out.verbose(f"Decoded parameter {self.which_param}={_r}.")
                    # A controller memory reference requires special handling.
                    if "mt" in self.param_list[self.which_param] and self.sub_command == 0x00:
                        # Suppress output — the reply handler produces the complete output.
                        if self.remaining == 0:
                            self._enter_state("mt_command")
                        else:
                            # More commands remain in this packet — continue
                            # processing them before entering memory transfer.
                            self._enter_state("expect_command")
                        return None
                    elif (
                        "index" in self.param_list[self.which_param]
                        and self.sub_command == 0x05
                    ):
                        self.decoded += " " + _r
                        self._enter_state("index_command")
                        return self.decoded
                    else:
                        self.decoded += " " + _r
                        # Advance to the next parameter.
                        _next = self.which_param + 1
                        self.cmd_values.append(self.decoder.value)
                        if _next >= len(self.param_list):
                            self.plot.cmd_update(
                                self.command_number,
                                self.label,
                                self.command,
                                self.sub_command,
                                self.cmd_values,
                            )
                            self.out.verbose("Parameters decoded.")
                            self._enter_state("expect_command")
                            return self.decoded
                        else:
                            self.which_param = _next
                            self._h_check_for_reply()
        return None

    def _tr_decode_parameters(self):
        """Prepare to parse a parameter. Prime the parameter decoder
        state machine."""
        self.which_param = 1
        if "mt" in self.param_list:
            self._enter_state("mt_command")
            return
        self.cmd_values = []
        self._h_check_for_reply()

    # ----

    # ++++
    def _st_decode_option(self, datum):
        """Get the option name from a lookup table."""
        if self._h_is_command(datum):
            self.out.error("Datum is command when should be an option.")
            self._forward_to_state("sync")
        if datum in self._options_lut:
            self.decoded = (
                f"0x{self.command:02X}{self.sub_command:02X}:{self._options_lut[datum]}"
            )
            self.label = self._options_lut[datum]
        else:
            self.out.error(f"Option 0x{datum:02X} is unknown.")
            self.decoded = f"Unknown option: {datum:02X}"
            self.label = f"OPT_{datum:02X}"
        self.param_list = None
        self.cmd_values = []
        self._enter_state("expect_command")
        return self.decoded

    def _tr_decode_option(self):
        """Prepare to lookup an option for a sub-command."""
        self._options_lut = self._ct[self.sub_command]

    # ----

    # ++++
    def _st_expect_sub_command(self, datum):
        """A command has been received which has a sub-command list."""
        if self.is_reply:
            self.out.error("Reply packet when expecting sub_command.")
            self._forward_to_state("mt_command")
        else:
            if self._h_is_command(datum):
                self.out.error("Datum is command when should be sub_command.")
                self._forward_to_state("sync")
            else:
                # Is it a known command for this state?
                if self._h_is_known_command(datum):
                    self.sub_command = datum
                    if self.command == rdap.SETTING and datum == rdap.SETTING_WRITE:
                        self._enable_checksum()
                    # Setting the file checksum signals the end of the checksum region.
                    if (
                        self.command == 0xE5
                        and self.sub_command is not None
                        and self.sub_command == 0x05
                    ):
                        self._disable_checksum()
                    _t = type(self._ct[datum])
                    if _t is str:
                        self.decoded = self.label = self._ct[datum]
                        self._enter_state("expect_command")
                        return self.decoded
                    elif _t is dict:
                        # A sub-command can select options.
                        self._enter_state("decode_option")
                    elif _t is tuple:
                        self.param_list = self._ct[datum]
                        self.decoded = self.label = self.param_list[0]
                        if self.param_list[1] == rdap.SKIP:
                            self._skip = self.param_list[2]
                        else:
                            self._enter_state("decode_parameters")
                    else:
                        # This is a problem with the protocol table -- not the
                        # incoming data.
                        self.out.protocol(
                            f"Unsupported or unexpected type ({_t}) in command."
                        )
                else:
                    self.out.critical(f"Datum 0x{datum:02X} is not a known command.")
                    self._forward_to_state("sync")
        return None

    def _tr_expect_sub_command(self):
        """Setup for a sub-command.

        NOTE: The data type MUST be a dict."""
        _t = type(self._ct[self.command])
        if _t is dict:
            self._ct = self._ct[self.command]
        else:
            # This is a problem with the protocol table -- not the incoming
            # data.
            self.out.protocol(
                f"Command table at 0x{self.command:02X} incorrect type {_t}."
            )
            self._enter_state("sync")

    # ----

    # ++++
    def _st_expect_command(self, datum):
        """Expect the incoming byte to be a command byte. If it is not then
        generate a protocol error and return to scanning for a command byte."""
        if self.is_reply:
            self.out.error("Reply packet when expecting command.")
            self._forward_to_state("mt_command")
        else:
            if self._h_is_command(datum):
                # Is it a known command for this state?
                if self._h_is_known_command(datum):
                    self.command = datum
                    if datum not in rdap.CHK_DISABLES:
                        self._enable_checksum()
                    _t = type(self._ct[datum])
                    if _t is str:
                        self.decoded = self.label = self._ct[datum]
                        if datum == rdap.EOF:
                            self._add_to_checksum(datum)
                            _i = self.decoder.checksum
                            _c = self.decoder.file_checksum
                            _d = self.decoder.checksum - self.decoder.file_checksum
                            _is = f"\n    decoded={self.decoder.checksum}"
                            _cs = f"\naccumulated={self.decoder.file_checksum}"
                            _ds = f"\ndifference ={_d}"
                            if not self._verify_checksum():
                                self.out.error(f"Checksum mismatch: {_is} {_cs} {_ds}")
                            else:
                                self.out.info(f"Checksum OK: {_i} {_c}")
                            self._reset_checksum()
                        self._enter_state("expect_command")
                        return self.decoded
                    elif _t is dict:
                        self._enter_state("expect_sub_command")
                    elif _t is tuple:
                        self.param_list = self._ct[datum]
                        self.decoded = self.label = self.param_list[0]
                        if self.param_list[1] == rdap.SKIP:
                            self._skip = self.param_list[2]
                        else:
                            self._enter_state("decode_parameters")
                    else:
                        # This is a problem with the protocol table -- not the
                        # incoming data.
                        self.out.error(
                            f"Unsupported or unexpected type ({_t}) in command."
                        )
                        self._enter_state("sync")
                else:
                    self.out.critical(f"Datum 0x{datum:02X} is not a known command.")
                    self._enter_state("sync")
            else:
                # Did not receive the expected command. This is either a problem
                # with the incoming stream or the protocol definition.
                self._enter_state("sync")

        return None

    def _tr_expect_command(self):
        self._h_prepare_for_command()

    # ----

    # ++++
    def _st_sync(self, datum):
        """Scan for a command byte to synchronize the parser with the input
        data.

        Once a command byte has been found normal command/reply processing
        begins.

        A command byte is the only byte which will have the most significant
        bit set."""
        if not self.is_reply:
            if self._h_is_command(datum):
                if self._h_is_known_command(datum):
                    self._forward_to_state("expect_command")
        return None

    def _tr_sync(self):
        self._h_prepare_for_command()

    # ----

    # ---------------

    def _report_parse(self, result: str, take: int = 0, remaining: int = 0):
        # Call here because the decoder decides when checksum is disabled.
        self._add_to_checksum(sum(self.host_bytes))
        # A command has been decoded.
        self.out.parser(
            f"T={take:04d} R={remaining:04d}"
            + f" SUM={self.decoder.file_checksum:08d}:\n{result}\n"
        )
        self.out.parser(f"cmd:{self.host_bytes.hex()}" + f" SUM={sum(self.host_bytes)}")
        self.out.parser(
            f"rep:{self.controller_bytes.hex()}" + f" SUM={sum(self.controller_bytes)}"
        )
        self.controller_bytes = bytearray([])
        self.host_bytes = bytearray([])

        # Fire the optional on_command callback for script generation.
        if self.on_command is not None:
            self.on_command(
                label=self.label,
                cmd_values=list(self.cmd_values),
                param_list=self.param_list,
                command=self.command,
                sub_command=self.sub_command,
                decoded=self.decoded,
                cmd_n=self.command_number,
            )

    def step(self, datum: int, is_reply=False, take: int = 0, remaining=0):
        """Step the state machine for the latest byte.

        Parameter:
            datum       The byte to step with.
            is_reply    True when the byte is from a reply whether that be an
                        ACK/NAK or reply data.
            take        The number of bytes taken from the input stream for the
                        current packet. This is used for verbose output.
            remaining   The number of bytes remaining in the current packet.
        """
        self.last = self.datum
        self.datum = datum
        self.last_is_reply = self.is_reply
        self.is_reply = is_reply
        self.remaining = remaining
        # Accumulate bytes.
        if self.is_reply:
            self.controller_bytes.append(datum)
        else:
            self.host_bytes.append(datum)
        # This is to skip anomalous data.
        if self._skip > 0:
            self._add_to_checksum(datum)
            self._skip -= 1
            self.out.warn(f"Skipping: 0x{datum:02X}")
            if self._skip <= 0:
                self._report_parse("End skip.", take, remaining)
                self._enter_state("expect_command")
        else:
            # Step the machine.
            _r = self._stepper(datum)
            if _r is not None:
                self._report_parse(_r, take, remaining)
        # Transitions only when a transition has been staged.
        self._next_state()
