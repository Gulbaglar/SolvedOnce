"""tinycsv: fictional package used by the SolvedOnce example packet.

It reproduces the bug described in examples/example-packet.json on purpose,
so the sandbox runner has something real to run. It is a stand-in for a
package mirror; it is not published anywhere.
"""

from ._fast import _split_line

__version__ = "3.1.1"


def reader(f, delimiter=",", lineterminator=None):
    """Yield rows from a file-like object.

    CRLF-terminated lines go through the fast path. Note that the fast path
    ignores `lineterminator`, as the example packet's failed attempts record.
    """
    for line in f:
        if line.endswith("\r\n"):
            yield _split_line(line, delimiter)
        else:
            yield line.rstrip("\n").split(delimiter)
