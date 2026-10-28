"""
Ruida Protocol Scripting Toolkit.

The rpascript package provides a human-readable scripting language (.rds files)
for generating and verifying Ruida protocol binary traffic.

Primary classes:
    - ScriptParser: parses .rds script files into command tuples
    - ScriptInterpreter: executes parsed commands, generating binary output
    - main: CLI entry point
"""

try:
    from ruida_pa.rpascript.interpreter import ScriptInterpreter, ScriptParser
except ImportError:
    # Submodules not yet available (skeleton stage)
    ScriptParser = None  # type: ignore
    ScriptInterpreter = None  # type: ignore

__all__ = ["ScriptParser", "ScriptInterpreter"]
