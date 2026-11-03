"""Fast path for CRLF-terminated files.

Fictional package used by the SolvedOnce example packet. It contains the
bug described in examples/example-packet.json on purpose.
"""


def _split_line(line, delimiter):
    """Split one CRLF-terminated line into fields."""
    line = line.rstrip('\r\n' + delimiter)
    return line.split(delimiter)
