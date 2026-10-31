"""Small terminal-aware layout helpers for the interactive setup wizard."""

import os
import sys
import textwrap


def bold(value: str) -> str:
    if sys.stdout.isatty() and os.environ.get("TERM") != "dumb" and "NO_COLOR" not in os.environ:
        return f"\033[1m{value}\033[0m"
    return value


def section(title: str) -> None:
    print(f"\n{bold(title)}\n{'─' * len(title)}\n")


def subsection(title: str) -> None:
    print(f"\n  {bold(title)}")


def note(message: str = "", *, indent: int = 2) -> None:
    for paragraph in message.splitlines() or [""]:
        number, separator, _ = paragraph.partition(". ")
        hanging = len(number) + 2 if separator and number.isdecimal() else 0
        print(textwrap.fill(paragraph, width=88, initial_indent=" " * indent,
                            subsequent_indent=" " * (indent + hanging), break_long_words=False,
                            break_on_hyphens=False) if paragraph else "")


def command(value: str, *, indent: int = 4) -> None:
    # Copyable commands must stay on one line, even in narrow terminals.
    print(" " * indent + value)
