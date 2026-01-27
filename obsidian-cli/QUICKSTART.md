# Obsidian CLI Framework - Quick Start

## Installation Complete ✓

The CLI framework has been successfully imported from ProcurementAgentAI and adapted for Obsidian vault use.

## Immediate Usage

```bash
# Reload your shell to activate the alias
source ~/.bash_aliases

# Or use the full path
/home/token/Obsidian/Token-ENV/obscli --help
```

## Quick Commands

```bash
# Show help
obscli --help

# Show CLI info
obscli info

# Example commands (demonstrating the pattern)
obscli example hello World
obscli example echo --count 3 "Hello!"

# Verbose mode
obscli --verbose info
```

## Creating Your First Command

### 1. Create a command file

Create `Scripts/cli/commands/vault.py`:

```python
"""Vault management commands."""

import click
from pathlib import Path


@click.group()
@click.pass_context
def vault_commands(ctx):
    """Obsidian vault management commands."""
    pass


@vault_commands.command()
@click.argument("workspace")
@click.pass_context
def workspace(ctx, workspace):
    """Show workspace information."""
    vault_root = Path("/home/token/Obsidian/Token-ENV")
    workspace_path = vault_root / f"{workspace}"

    if workspace_path.exists():
        click.echo(f"✓ Workspace '{workspace}' exists")
        click.echo(f"  Path: {workspace_path}")
    else:
        click.echo(f"✗ Workspace '{workspace}' not found")


@vault_commands.command()
@click.pass_context
def list_workspaces(ctx):
    """List all workspace folders."""
    vault_root = Path("/home/token/Obsidian/Token-ENV")

    # Find numbered workspace folders
    workspaces = sorted([d for d in vault_root.iterdir()
                        if d.is_dir() and d.name[0].isdigit()])

    click.echo("Obsidian Workspaces:")
    for ws in workspaces:
        click.echo(f"  • {ws.name}")
```

### 2. Register the command

Edit `Scripts/cli/main.py` and add:

```python
from Scripts.cli.commands.vault import vault_commands
cli.add_command(vault_commands, name="vault")
```

### 3. Use your new commands

```bash
obscli vault list-workspaces
obscli vault workspace 1-Obsidian
```

## Integration Points

### With Shell Scripts

```bash
#!/bin/bash
# Use CLI in shell scripts
result=$(obscli vault workspace 1-Obsidian)
echo "$result"
```

### With AutoHotkey

```ahk
; Call CLI from AHK v2
Run("obscli vault list-workspaces", , "Hide")
```

### With Templater

````markdown
```javascript
// In Templater template
const { execSync } = require('child_process');
const result = execSync('obscli vault list-workspaces').toString();
return result;
```
````

## Next Steps

1. **Read the full documentation**: `Scripts/cli/README.md`
2. **Explore example commands**: `Scripts/cli/commands/example.py`
3. **Create custom commands** for your vault workflows
4. **Add utilities** in `Scripts/cli/utils/`

## Common Use Cases

### Vault Operations
- Create notes with templates
- Move notes between workspaces
- Update frontmatter in bulk
- Generate reports

### Journal System
- Create daily notes
- Update habit trackers
- Pull context from previous days
- Archive completed tasks

### Automation
- Integrate with external APIs
- Process markdown files
- Generate documentation
- Sync with external systems

## Architecture

```
Token-ENV/
├── obscli                    # Main executable
└── Scripts/
    └── cli/
        ├── main.py          # Entry point & CLI group
        ├── config.py        # Environment configuration
        ├── commands/        # Command modules
        │   ├── example.py   # Example commands
        │   └── [your commands here]
        └── utils/           # Helper functions
            └── [your utilities here]
```

## Differences from ProcurementAgentAI CLI

This framework **excludes**:
- API client functionality
- Specific business commands (client, kb, google-drive, test)
- Environment-specific URLs and authentication

This framework **includes**:
- Core CLI structure (Click-based)
- Modular command system
- Environment configuration
- Example command patterns

## Support

For CLI framework issues, see `Scripts/cli/README.md`

For Obsidian vault integration, see the main CLAUDE.md in the vault root.
