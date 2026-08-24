"""Shared terminal styling for the flexgate CLI.

All colored output funnels through this module so the NO_COLOR / non-TTY
gating lives in exactly one place. `bold`/`dim`/... return plain text when
stdout is not a terminal (piped/redirected output stays clean), and `err()`
writes to stderr — the color decision still follows stdout's tty state,
which is fine for the usual "both are the same terminal" case.
"""

from __future__ import annotations

import argparse
import os
import sys

_TTY = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _st(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if _TTY else text


def bold(t: str) -> str: return _st(t, "1")
def dim(t: str) -> str: return _st(t, "2")
def red(t: str) -> str: return _st(t, "31")
def green(t: str) -> str: return _st(t, "32")
def yellow(t: str) -> str: return _st(t, "33")
def magenta(t: str) -> str: return _st(t, "35")
def cyan(t: str) -> str: return _st(t, "36")


def ok(msg: str) -> None:
    print(f"{green('✓')} {msg}")


def warn(msg: str) -> None:
    print(f"{yellow('!')} {msg}")


def fail(msg: str) -> None:
    print(f"{red('✗')} {msg}")


def err(msg: str, exit_code: int | None = None) -> None:
    print(red(msg), file=sys.stderr)
    if exit_code is not None:
        sys.exit(exit_code)


class FlexgateHelpFormatter(argparse.HelpFormatter):
    """Colorized argparse help: bold flags/commands, dim usage prefix and
    section headings.

    argparse's own help theming (Python 3.14+) is disabled so the look and
    the NO_COLOR / non-TTY gating match flexgate's own styling on every
    Python version.
    """

    _HEADINGS = ("usage:", "options:", "commands:", "positional arguments:", "arguments:")

    def _set_color(self, color):
        # Parser-level color kwarg (3.14) re-enables theming after __init__;
        # keep it off unconditionally.
        try:
            super()._set_color(False)
        except AttributeError:  # Python < 3.14 has no theming
            pass

    def add_usage(self, usage, actions, groups, prefix=None):
        if prefix is None:
            prefix = "usage: "
        super().add_usage(usage, actions, groups, dim(prefix))

    def _format_action_invocation(self, action):
        return bold(super()._format_action_invocation(action))

    def format_help(self):
        help_text = super().format_help()
        for heading in self._HEADINGS:
            if help_text.startswith(heading):
                help_text = bold(heading) + help_text[len(heading):]
            help_text = help_text.replace(f"\n{heading}", f"\n{bold(heading)}")
        return help_text


class FlexgateParser(argparse.ArgumentParser):
    """ArgumentParser prewired to FlexgateHelpFormatter.

    Subparsers created via ``add_subparsers().add_parser(...)`` inherit this
    class automatically (argparse defaults ``parser_class`` to the parent's
    type), so one use at the root parser styles every help screen.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", FlexgateHelpFormatter)
        try:
            super().__init__(*args, **kwargs, color=False)
        except TypeError:  # Python < 3.14 has no color kwarg
            super().__init__(*args, **kwargs)
