"""Observe prompt_toolkit's actual rendered stream, not its queued print request."""
from copy import copy

from agent.native_emission import capture_context, observed_stream


def print_ansi(text):
    from cli import _PT_ANSI, _pt_print
    if capture_context() is None:
        return _pt_print(_PT_ANSI(text))
    from prompt_toolkit.application.current import get_app_session
    from prompt_toolkit.output.vt100 import Vt100_Output
    from prompt_toolkit.output.plain_text import PlainTextOutput
    output = get_app_session().output
    # These native outputs flush a TextIO stream. Dummy/custom/Win32 outputs
    # have no qualified write boundary here; returning from print is not proof.
    if type(output) not in (Vt100_Output, PlainTextOutput) or output._buffer:
        return _pt_print(_PT_ANSI(text))
    stream = observed_stream(output.stdout, surface="cli.rendered", operation="render")
    if stream is output.stdout:
        return _pt_print(_PT_ANSI(text))
    local = copy(output)
    local.stdout = stream
    local._buffer = []
    from hermes_cli.cli_origin import map_prompt_output
    from agent.native_emission import guard_emissions
    map_prompt_output(local, stream, text)
    with guard_emissions(lambda: get_app_session().output is output and output.stdout is stream._stream):
        return _pt_print(_PT_ANSI(text), output=local)


def print_fallback(text):
    import sys
    stream = observed_stream(sys.stdout, surface="cli.rendered", operation="fallback")
    print(text, file=stream)
    if stream is not sys.stdout:
        stream.flush()
