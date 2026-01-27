# Installation Guide

## Quick Setup

The CLI tools are designed to work automatically from any repository. To make them available system-wide:

### Option 1: Add to PATH (Recommended)

Add this line to your `~/.bashrc` or `~/.zshrc`:

```bash
export PATH="$HOME/cli-tools/bin:$PATH"
```

Then reload your shell:
```bash
source ~/.bashrc  # or source ~/.zshrc
```

### Option 2: Create Symlinks

```bash
sudo ln -s ~/cli-tools/bin/subagent /usr/local/bin/subagent
sudo ln -s ~/cli-tools/bin/time-convert /usr/local/bin/time-convert
```

### Option 3: Use Direct Paths

You can always call the tools directly:
```bash
~/cli-tools/bin/subagent "Your prompt"
~/cli-tools/bin/time-convert 930 America/Los_Angeles
```

## For AI Agents (Non-Interactive Shells)

AI agents can use the tools directly via full paths. The wrapper script handles venv management automatically:

```bash
# From any repository
$HOME/cli-tools/bin/subagent "Investigate issue X"
$HOME/cli-tools/bin/time-convert 1500 Europe/Berlin --verbose
```

The tools will:
1. Automatically detect if a venv exists
2. Create one if needed using `uv`
3. Install/update dependencies
4. Execute the command

When running `subagent`, the command also inspects `UV_PROJECT_ENVIRONMENT`, local `.venv/venv` folders, or an explicit `CLI_TOOLS_CODEX_VENV` path to launch Codex from the same packaged environment as the repo even if it is not currently activated.

## Verification

Test the installation:

```bash
subagent --help
time-convert --help
```

## Requirements

- Python 3.11+
- `uv` package manager (install with: `curl -LsSf https://astral.sh/uv/install.sh | sh`)

## Troubleshooting

### Command not found
- Ensure `~/cli-tools/bin` is in your PATH
- Or use full paths: `~/cli-tools/bin/subagent` or `~/cli-tools/bin/time-convert`

### uv not found
- Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Or set `UV_INSTALL_DIR` if installed elsewhere

### Permission denied
- Make scripts executable: `chmod +x ~/cli-tools/bin/*`
