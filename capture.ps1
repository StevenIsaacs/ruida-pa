# Use this script to run tshark to capture a log file which can then be processed
# using decode.
#
# Parameters:
#  if       The interface to capture on (typically Ethernet)
#  ip       The Ruida controller IP address.
#  out      The capture file name -- not including extension. This saves to $2.log.
#  Protocol udp (default) or tcp -- tcp captures the Ruida TCP stream on port 50200.
#           rpc -- rpc captures the TUI RPyC RPC server stream (default port 18812).
#  RpcPort  The RPC port to capture (default 18812). Only valid with -Protocol rpc.
#
# NOTE: For localhost RPC traffic on Windows, -if must be the Npcap loopback
# adapter (e.g. "Adapter for loopback traffic capture"); Windows has no "any".
#
# NOTE: -Protocol rpc captures the binary RPyC protocol between the TUI RPC server
# and a client. The resulting .log is not decodable by rpa.py (which understands
# only the Ruida protocol); it is intended for external inspection (e.g. Wireshark).
# WARNING: No parameter checking.
param (
	[string]$if,     # The interface to capture on.
	[string]$ip,     # IP address of controller.
	[string]$out,    # Output file name (excluding extension).
	[ValidateSet("udp", "tcp", "rpc")]
	[string]$Protocol = "udp",
	[int]$RpcPort = 18812
)
if ($Protocol -eq "rpc") {
	$filter = "(ip.addr == $ip && tcp.port == $RpcPort)"
	$fields = @("-e", "frame.time_delta", "-e", "tcp.srcport", "-e", "tcp.dstport", "-e", "tcp.len", "-e", "tcp.payload")
} elseif ($Protocol -eq "tcp") {
	$filter = "(ip.addr == $ip && tcp.port == 50200)"
	$fields = @("-e", "frame.time_delta", "-e", "tcp.srcport", "-e", "tcp.dstport", "-e", "tcp.len", "-e", "tcp.payload")
} else {
	$filter = "(ip.addr == $ip)"
	$fields = @("-e", "frame.time_delta", "-e", "udp.port", "-e", "udp.length", "-e", "data.data")
}
tshark -Y $filter -i $if -l -T fields @fields | tee $out
