"""Parser for Tecomat/iNELS `.pub` files."""

from __future__ import annotations

import re
from pathlib import Path

from .models import PubEntry

BIT_RE = re.compile(r"^\.(?P<bit>\d+)$")


class PubFileParseError(ValueError):
    """Raised when a `.pub` file contains malformed content."""


def parse_pub_line(line: str, *, line_number: int) -> PubEntry | None:
    """Parse one `.pub` file line into a :class:`PubEntry`."""
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", ";")):
        return None

    parts = stripped.split()
    if len(parts) == 6:
        name, register, storage, address_text, data_type, access = parts
        bit = None
    elif len(parts) == 7:
        name, register, storage, address_text, bit_text, data_type, access = parts
        bit_match = BIT_RE.match(bit_text)
        if bit_match is None:
            raise PubFileParseError(f"Malformed .pub line {line_number}: {line.rstrip()}")
        bit = int(bit_match.group("bit"))
    else:
        raise PubFileParseError(f"Malformed .pub line {line_number}: {line.rstrip()}")

    if not address_text.isdigit() or not access.startswith("PUB_"):
        raise PubFileParseError(f"Malformed .pub line {line_number}: {line.rstrip()}")

    return PubEntry(
        name=name,
        register=register,
        storage=storage,
        address=int(address_text),
        bit=bit,
        data_type=data_type,
        access=access,
        line_number=line_number,
    )


def parse_pub_text(text: str) -> tuple[PubEntry, ...]:
    """Parse a full `.pub` file content string."""
    parsed_entries: list[PubEntry] = []
    pending_line: tuple[int, str] | None = None

    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue

        if pending_line is not None:
            start_line, pending_text = pending_line
            entry = None
            for candidate in (pending_text + stripped, pending_text + " " + stripped):
                try:
                    entry = parse_pub_line(candidate, line_number=start_line)
                except PubFileParseError:
                    continue
                break
            if entry is None:
                raise PubFileParseError(
                    f"Malformed wrapped .pub line {start_line}-{line_number}: "
                    f"{pending_text!r} + {line.rstrip()!r}"
                )
            parsed_entries.append(entry)
            pending_line = None
            continue

        try:
            entry = parse_pub_line(line, line_number=line_number)
        except PubFileParseError:
            pending_line = (line_number, stripped)
            continue

        if entry is not None:
            parsed_entries.append(entry)

    if pending_line is not None:
        line_number, pending_text = pending_line
        raise PubFileParseError(f"Malformed .pub line {line_number}: {pending_text}")

    return tuple(parsed_entries)


def load_pub_entries(path: str | Path) -> tuple[PubEntry, ...]:
    """Load and parse a `.pub` file from disk."""
    file_path = Path(path).expanduser()
    return parse_pub_text(file_path.read_text(encoding="utf-8"))
