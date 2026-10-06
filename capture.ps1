# Use this script to run tshark to capture a log file which can then be processed
# using decode.
#
# Parameters:
#  if       The interface to capture on (typically Ethernet)
#  ip       The Ruida controller IP address.
#  out      The capture file name -- not including extension. This saves to $2.log.
#  Protocol udp (default) or tcp -- tcp captures the Ruida TCP stream on port 50200.
# WARNING: No parameter checking.
param (
	[string]$if,     # The interface to capture on.
	[string]$ip,     # IP address of controller.
	[string]$out,    # Output file name (excluding extension).
	[ValidateSet("udp", "tcp")]
	[string]$Protocol = "udp"
)
if ($Protocol -eq "tcp") {
	$filter = "(ip.addr == $ip && tcp.port == 50200)"
	$fields = @("-e", "frame.time_delta", "-e", "tcp.srcport", "-e", "tcp.dstport", "-e", "tcp.len", "-e", "tcp.payload")
} else {
	$filter = "(ip.addr == $ip)"
	$fields = @("-e", "frame.time_delta", "-e", "udp.port", "-e", "udp.length", "-e", "data.data")
}
tshark -Y $filter -i $if -l -T fields @fields | tee $out
