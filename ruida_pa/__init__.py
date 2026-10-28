"""Ruida Protocol Analyzer (ruida-pa).

Single top-level namespace consolidating every ruida-pa module (issue #14):

    ruida_pa.protocols    protocol state machine, parser, command tables
    ruida_pa.rpalib       output emission, line parsing, plotting, RPC
    ruida_pa.rpascript    .rds script parsing/generation and the TUI
    ruida_pa.ruidadriver  controller driver layer
    ruida_pa.rpa          the `rpa` CLI module

The previous top-level names (rpa, protocols, rpalib, rpascript,
ruidadriver) remain importable through deprecation shims during the
transition and will be removed in a future release.
"""

from ruida_pa.rpalib.version import __version__

__all__ = ["__version__"]
