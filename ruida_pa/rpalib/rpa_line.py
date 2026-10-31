import math


class RpaLine:
    """A line representing a move of the virtual head."""

    def to_length(self, start: tuple[float, float], end: tuple[float, float]):
        _line_x_ends = (start[0], end[0])
        _line_x_len = abs(_line_x_ends[0] - _line_x_ends[1])
        _line_y_ends = (start[1], end[1])
        _line_y_len = abs(_line_y_ends[0] - _line_y_ends[1])
        return math.hypot(_line_x_len, _line_y_len)

    def __init__(
        self, cmd_id, cmd_label, index, start, end, speed, power, width, style, color
    ):
        self.cmd_id = cmd_id  # For sanity.
        self.command = cmd_label
        self.index = index
        self.start = start
        self.end = end
        self.length = self.to_length(start, end)
        self.speed = speed
        self.power = power
        self.width = width
        self.style = style
        self.color = color

    def __str__(self):
        return (
            f"\nCmd ID: {self.cmd_id}"
            f"\nCommand: {self.command}"
            f"\nIndex: {self.index}"
            f"\nStart: {self.start}"
            f"\nEnd: {self.end}"
            f"\nLength: {self.length}mm"
            f"\nSpeed: {self.speed}mm/S"
            f"\nPower: {self.power}%"
            f"\nLine width: {self.width}"
            f"\nLine style: {self.style}"
            f"\nColor: {self.color}"
            " #".join(f"{int(_c * 255):02X}" for _c in self.color)
        )

    @property
    def annotation(self) -> str:
        """Generate the string for a line annotation box to be displayed
        on a plot."""
        _start = tuple(f"{x:.3f}mm" for x in self.start)
        _end = tuple(f"{x:.3f}mm" for x in self.end)
        return (
            f"{self.cmd_id}:{self.command}\n"
            f"\nstart={_start}"
            f"\nend={_end}"
            f"\nLength: {self.length:.3f}mm"
            f"\nPower={self.power:.1f}%"
            f"\nSpeed={self.speed:.1f}mm/S"
        )
