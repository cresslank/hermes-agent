"""Origin mapping through native Rich segments and prompt_toolkit writes.

Only native marked literal text qualifies. Rich soft wrapping may elide spaces;
its ordered segment projection proves that mapping before any line is written.
No output buffer, history search or unmarked text can acquire provenance.
"""
import uuid

from rich.console import Console
from rich.segment import Segment
from rich.style import Style

from agent.supervision_original_output import origin_of, codec


class _Literal:
    def __init__(self, rendered, original):
        self.rendered, self.original = rendered, original

    def __rich_measure__(self, console, options):
        from rich.measure import Measurement
        return Measurement.get(console, options, self.rendered)

    def __rich_console__(self, console, options):
        origin = origin_of(self.original)
        if origin is None or not isinstance(console, OriginConsole):
            yield self.rendered
            return
        console.origins[origin.id] = self.original
        for segment in console.render(self.rendered, options):
            if segment.control:
                yield segment
            else:
                yield Segment(segment.text, (segment.style or Style()) + Style(meta={"native_original": origin.id}))


def literal_renderable(rendered, original):
    return _Literal(rendered, original) if origin_of(original) is not None and rendered.plain == original else rendered


class _MappedLine(str):
    def __new__(cls, ansi, original, plain, mapping):
        obj = super().__new__(cls, ansi)
        obj.original, obj.plain, obj.mapping = original, plain, mapping
        return obj


class OriginConsole(Console):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.origins = {}
        self.origin_lines = []

    def _render_buffer(self, buffer):
        ansi = super()._render_buffer(buffer)
        self.origin_lines = []
        origins, self.origins = self.origins, {}
        if not origins or len(ansi) > 16384:
            return ansi
        lines = ansi.rstrip("\n").split("\n")
        segments = list(Segment.split_lines(buffer))
        if len(lines) != len(segments) or len(origins) != 1:
            return ansi
        ident, original = next(iter(origins.items()))
        if "\n" in original or any(ord(c) < 32 for c in original):
            return ansi
        mapped, cursor = [], 0
        for index, parts in enumerate(segments):
            plain, positions = "", []
            for part in parts:
                mark = (part.style.meta or {}).get("native_original") if part.style else None
                if mark == ident:
                    positions.append((len(plain), len(plain) + len(part.text)))
                plain += part.text
            if not positions:
                continue
            start, end = positions[0][0], positions[-1][1]
            if (any(ord(c) < 32 for c in plain)
                    or not all(a[1] == b[0] for a, b in zip(positions, positions[1:]))):
                return ansi
            # Rich owns these marked segments. Padding and soft-wrap space
            # elision are the only permitted differences, never substring search.
            piece = plain[start:end].rstrip(" ")
            if not piece:
                return ansi
            prior = cursor
            if not original.startswith(piece, cursor):
                while cursor < len(original) and original[cursor] == " ":
                    cursor += 1
            if not original.startswith(piece, cursor):
                return ansi
            stop = cursor + len(piece)
            mapped.append((index, plain, start, start + len(piece), prior, cursor, stop))
            cursor = stop
        if cursor != len(original) or not 0 < len(mapped) <= 32:
            return ansi
        group = uuid.uuid4().hex
        self.origin_lines = list(lines)
        for ordinal, (index, plain, start, end, prior, cursor, stop) in enumerate(mapped):
            mapping = ("native_text_chars", group, ordinal, len(mapped), start, end, prior, cursor, stop)
            self.origin_lines[index] = _MappedLine(lines[index], original, plain, mapping)
        return ansi


def map_prompt_output(local, stream, line):
    """Snapshot native PT text writes and its exact buffer BEFORE native flush.

    Styles/control sequences come only from this renderer. We do not strip ANSI
    from arbitrary output then search it for a coincidental source substring.
    """
    from agent.supervision_original_output import native_text_sink
    if type(line) is not _MappedLine or not native_text_sink(stream._stream):
        return
    native_write, native_flush = local.write, local.flush
    expected = line.plain + "\r\n"
    position = 0
    valid = True

    def write(text):
        nonlocal valid, position
        valid = valid and text == expected[position:position + len(text)]
        position += len(text)
        return native_write(text)

    def flush():
        nonlocal valid
        outgoing = "".join(local._buffer)
        # PT flushes before starting a print, too. An empty preflush is not the
        # literal segment's completion and must not consume its mapping.
        if position == 0:
            return native_flush()
        if valid and position == len(expected):
            stream._origin = codec(line.original, outgoing, "cli.literal-line.v1", line.mapping)
        else:
            stream._origin = None
        valid = False
        return native_flush()

    local.write, local.flush = write, flush
