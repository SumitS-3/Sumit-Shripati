"""Atomic UTF-8 text output shared by CSV and HTML deliverables."""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
from typing import Iterator, TextIO


@contextmanager
def atomic_text(path: str | Path) -> Iterator[TextIO]:
    # Replace the output entry itself; never follow an existing file symlink and
    # accidentally overwrite its source target.
    destination = Path(os.path.abspath(path))
    descriptor, temporary = tempfile.mkstemp(suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            yield stream
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
