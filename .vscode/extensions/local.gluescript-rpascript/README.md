# GlueScript & Ruida Script

A zero-build VSCode **workspace extension** providing syntax highlighting,
language configuration, and code snippets for the Ruida PA project's two
script formats:

- **GlueScript (`.cglu`)** — the high-level job scripting transcript used by
  the `/gluescript` TUI command.
- **Ruida Script (`.rds`)** — the human-readable Ruida discovery script format
  used by `rpascript`.

## Install

This is a VSCode **workspace extension**. When this repository is opened as
the workspace root, VSCode detects the `.vscode/extensions/` folder and
prompts to install the extension into the workspace. After the one-time
install it auto-loads whenever this workspace is opened.

## Features

- **Syntax highlighting** for `.cglu` and `.rds` files.
- **Language configuration** — comment markers, brackets, and auto-closing
  pairs tuned to each format.
- **Snippets** for common GlueScript methods and Ruida Script commands.

## Verified Mnemonics

Ruida Script (`.rds`) files can render **verified** MT/CT mnemonics in green.
A mnemonic is *verified* when its declaration line in
`protocols/ruida/ruida_protocol.py` carries a `# Verified <source>` comment.

The mechanism:

- The verified list lives in the `verified` repository rule of
  `syntaxes/rpascript.tmLanguage.json`.
- Verified mnemonics are scoped `support.function.verified.rpascript`.
- The **Ruida Verified** theme (`themes/ruida-verified.json`) colors that
  scope `#00FF00`.

> **Warning:** selecting the **Ruida Verified** theme replaces your ENTIRE
> editor theme — it is a full dark theme that only customizes the verified
> mnemonic color.

**Alternative without switching themes** — add to your `settings.json`:

```json
"editor.tokenColorCustomizations": {
  "textMateRules": [
    {
      "scope": "support.function.verified.rpascript",
      "settings": { "foreground": "#00FF00" }
    }
  ]
}
```

All mnemonics are currently **unverified**, so nothing renders green yet; as
mnemonics are verified, the list in the grammar rule grows.

**Known limitation:** mnemonics on `CORE`/`CMD`-prefixed lines and names
shadowed by other grammar rules (`EOF`, `session`/`server`/`new_packet`/
`delay`/`wait`, `MACHINE_STATUS_*`, `MACHINE`/`CURRENT`/`ABSOLUTE`/
`SET_POINT`) will not render green.

## File Associations

| Extension | Language ID  | Grammar             |
| --------- | ------------ | ------------------- |
| `.cglu`   | `gluescript` | `source.gluescript` |
| `.rds`    | `rpascript`  | `source.rpascript`  |

> Note: `.rd` files are deliberately **not** associated — `.rd` is a binary
> RDWorks format, not Ruida Script text.

## Samples

- `samples/demo.cglu` — a canonical, valid GlueScript transcript.
- `samples/demo.rds` — a canonical Ruida Script example.
