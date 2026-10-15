"""Incremental extraction of one string field from a JSON object that is still arriving.

The chat loop needs the model to answer with a JSON object (``reply``, ``tool_calls``,
``create_task``), so the raw token stream cannot be shown to the operator: half of a JSON
document is not text anyone should read. But the ``reply`` value *is* prose, and it can
be shown the moment its characters arrive.

`JsonStringFieldStreamer` watches the stream and hands back only the decoded characters of
one top-level string field, as they become available. It never needs the document to be
complete, and it never changes what the caller parses afterwards: the loop still collects
the full body and runs `parse_json` on it, exactly as it does without streaming.

It is deliberately forgiving. Text before the opening brace (prose, a code fence) is
skipped; escapes, ``\\uXXXX`` sequences and surrogate pairs are decoded; keys other than
the wanted one, and their nested values, are skipped without being interpreted. If the
stream is not shaped like an object at all it simply yields nothing, and the caller's
final full-body result is still shown.
"""

from __future__ import annotations

from typing import Optional

_SIMPLE_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
_REPLACEMENT = "\ufffd"


class JsonStringFieldStreamer:
    """Feed it text chunks; it returns the new decoded characters of ``field``."""

    def __init__(self, field: str = "reply") -> None:
        self.field = field
        self._started = False  # the top-level '{' has been seen
        self._depth = 0
        self._expect_key = False
        self._collecting_key = False
        self._key_buf: list[str] = []
        self._key_escape = False
        self._after_key = False
        self._after_colon = False
        self._current_key = ""
        self._in_string = False  # inside a string that is being skipped
        self._skip_escape = False
        self._in_target = False  # inside the wanted string, emitting characters
        self._escape = False
        self._unicode: Optional[list[str]] = None
        self._high_surrogate: Optional[int] = None
        self._done = False  # the wanted field has been fully read
        self._finished = False  # the top-level object has closed

    @property
    def done(self) -> bool:
        """True once the wanted field has ended (or the whole object has closed)."""
        return self._done or self._finished

    # ------------------------------------------------------------------
    def feed(self, chunk: str) -> str:
        """Consume ``chunk`` and return whatever part of the field it completed."""
        out: list[str] = []
        for char in chunk:
            if self._finished:
                break
            if self._in_target:
                self._target_char(char, out)
            elif self._in_string:
                self._skipped_string_char(char)
            else:
                self._structural_char(char)
        return "".join(out)

    # ------------------------------------------------------------------
    def _emit(self, text: str, out: list[str]) -> None:
        if self._high_surrogate is not None:
            # A high surrogate that was not followed by a low one is not a character.
            out.append(_REPLACEMENT)
            self._high_surrogate = None
        out.append(text)

    def _target_char(self, char: str, out: list[str]) -> None:
        if self._unicode is not None:
            self._unicode.append(char)
            if len(self._unicode) < 4:
                return
            digits = "".join(self._unicode)
            self._unicode = None
            try:
                code = int(digits, 16)
            except ValueError:
                self._emit(_REPLACEMENT, out)
                return
            if 0xD800 <= code <= 0xDBFF:
                if self._high_surrogate is not None:
                    out.append(_REPLACEMENT)
                self._high_surrogate = code
            elif 0xDC00 <= code <= 0xDFFF and self._high_surrogate is not None:
                combined = 0x10000 + ((self._high_surrogate - 0xD800) << 10) + (code - 0xDC00)
                self._high_surrogate = None
                out.append(chr(combined))
            elif 0xDC00 <= code <= 0xDFFF:
                self._emit(_REPLACEMENT, out)
            else:
                self._emit(chr(code), out)
            return

        if self._escape:
            self._escape = False
            if char == "u":
                self._unicode = []
            else:
                self._emit(_SIMPLE_ESCAPES.get(char, char), out)
            return

        if char == "\\":
            self._escape = True
            return

        if char == '"':
            if self._high_surrogate is not None:
                out.append(_REPLACEMENT)
                self._high_surrogate = None
            self._in_target = False
            self._done = True
            self._after_colon = False
            return

        self._emit(char, out)

    def _skipped_string_char(self, char: str) -> None:
        if self._skip_escape:
            self._skip_escape = False
            if self._collecting_key:
                self._key_buf.append(char)
            return
        if char == "\\":
            self._skip_escape = True
            if self._collecting_key:
                self._key_buf.append(char)
            return
        if char == '"':
            self._in_string = False
            if self._collecting_key:
                self._collecting_key = False
                self._current_key = "".join(self._key_buf)
                self._key_buf = []
                self._after_key = True
            return
        if self._collecting_key:
            self._key_buf.append(char)

    def _structural_char(self, char: str) -> None:
        if not self._started:
            if char == "{":
                self._started = True
                self._depth = 1
                self._expect_key = True
            return

        if char == '"':
            if self._depth == 1 and self._expect_key:
                self._expect_key = False
                self._collecting_key = True
                self._key_buf = []
                self._in_string = True
            elif (
                self._depth == 1
                and self._after_colon
                and self._current_key == self.field
                and not self._done
            ):
                self._after_colon = False
                self._in_target = True
            else:
                self._after_colon = False
                self._in_string = True
            return

        if char == ":" and self._depth == 1 and self._after_key:
            self._after_key = False
            self._after_colon = True
            return

        if char in "{[":
            self._depth += 1
            self._after_colon = False
            return

        if char in "}]":
            self._depth -= 1
            if self._depth <= 0:
                self._finished = True
            return

        if char == "," and self._depth == 1:
            self._expect_key = True
            self._after_colon = False
            return

        if not char.isspace() and self._after_colon:
            # A number, true/false/null: the value of the key, not a string.
            self._after_colon = False


__all__ = ["JsonStringFieldStreamer"]
