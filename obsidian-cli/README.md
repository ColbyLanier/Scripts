# Obsidian CLI Framework

A modular command-line interface framework for creating custom bash commands, adapted from the ProcurementAgentAI CLI system.

## Features

- **Modular Command Structure**: Organize commands into logical groups
- **Environment Support**: Multiple environment configurations
- **Verbose Mode**: Detailed output for debugging
- **Click-Based**: Built on the powerful Click library for Python CLIs
- **Extensible**: Easy to add new commands and utilities

## Installation

### Prerequisites

```bash
# Install Click if not already installed
pip install click
```

### Setup

```bash
cd /home/token/Obsidian/Token-ENV/Scripts/cli
python setup_cli.py
```

This will create an `obscli` wrapper script in the Token-ENV root directory.

### Adding to PATH

Add to your `~/.bash_aliases`:

```bash
# Obsidian CLI
export PATH="/home/token/Obsidian/Token-ENV:$PATH"
# Or use an alias
alias obscli="/home/token/Obsidian/Token-ENV/obscli"
```

## Usage

### Basic Commands

```bash
# Show help
obscli --help

# Show CLI info
obscli info

# Use verbose mode
obscli --verbose info

# Example commands
obscli example hello World
obscli example echo --count 3 "Hello!"
```

### Environment Support

```bash
# Use specific environment
obscli --env production info
```

## Creating Custom Commands

### 1. Create a Command Module

Create a new file in `Scripts/cli/commands/`, e.g., `mycommands.py`:

```python
"""My custom commands."""

import click


@click.group()
@click.pass_context
def my_commands(ctx):
    """My custom command group."""
    pass


@my_commands.command()
@click.argument("name")
@click.pass_context
def greet(ctx, name):
    """Greet someone."""
    config = ctx.obj["config"]

    if config.verbose:
        click.echo(f"Running in {config.env_config.name} environment")

    click.echo(f"Greetings, {name}!")


@my_commands.command()
@click.option("--format", type=click.Choice(["json", "text"]), default="text")
@click.pass_context
def status(ctx, format):
    """Show status in different formats."""
    if format == "json":
        click.echo('{"status": "ok"}')
    else:
        click.echo("Status: OK")
```

### 2. Register the Command Group

Edit `Scripts/cli/main.py` and add:

```python
# Import your command group
from Scripts.cli.commands.mycommands import my_commands

# Register it (before if __name__ == "__main__")
cli.add_command(my_commands, name="my")
```

### 3. Use Your Commands

```bash
obscli my greet Alice
obscli my status --format json
```

## Directory Structure

```
Scripts/cli/
├── __init__.py           # Package initialization
├── main.py               # Main CLI entry point
├── config.py             # Configuration management
├── setup_cli.py          # Setup script
├── README.md             # This file
├── commands/             # Command modules
│   ├── __init__.py
│   └── example.py        # Example command group
└── utils/                # Utility functions
    └── __init__.py
```

## Command Pattern

All commands follow this pattern:

1. **Command Group**: A `@click.group()` decorated function
2. **Commands**: `@command_group.command()` decorated functions
3. **Context**: Access config via `ctx.obj["config"]`
4. **Arguments/Options**: Use Click decorators for parameters

## Environment Configuration

Edit `config.py` to add environments:

```python
ENVIRONMENTS: Dict[str, EnvironmentConfig] = {
    "default": EnvironmentConfig(
        name="default",
        description="Default environment",
    ),
    "production": EnvironmentConfig(
        name="production",
        description="Production environment",
    ),
}
```

## Utilities

Add helper functions in `Scripts/cli/utils/`:

```python
# Scripts/cli/utils/helpers.py
"""Helper utilities."""

def format_output(data, format_type="text"):
    """Format output in different ways."""
    if format_type == "json":
        import json
        return json.dumps(data, indent=2)
    return str(data)
```

Use in commands:

```python
from Scripts.cli.utils.helpers import format_output

@my_commands.command()
def show_data(ctx):
    data = {"key": "value"}
    click.echo(format_output(data, "json"))
```

## Integration with Obsidian Vault

This CLI framework is designed to integrate with the Obsidian vault automation:

- Create commands for note management
- Automate workspace operations
- Integrate with existing shell scripts
- Call from AutoHotkey scripts
- Use in templates via Templater

## Obsidian Integration

The CLI includes comprehensive Obsidian Advanced URI integration:

```bash
# Open daily note
obscli obs daily

# Open file at specific location
obscli obs open "Projects/Project.md" --heading "Tasks"

# Load workspace
obscli obs workspace "1-Obsidian"

# Quick workspace shortcuts
obscli obs quick ws 1

# Write content
obscli obs write "file.md" "New content"

# Execute commands
obscli obs command "app:reload"
```

See [OBSIDIAN-COMMANDS.md](OBSIDIAN-COMMANDS.md) for complete documentation.

## Examples

See `commands/example.py` for a complete example of command patterns.

## Troubleshooting

### Import Errors

If you get import errors, ensure the project root is in your Python path:

```python
import sys
from pathlib import Path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))
```

### Permission Denied

Make sure scripts are executable:

```bash
chmod +x /home/token/Obsidian/Token-ENV/obscli
chmod +x /home/token/Obsidian/Token-ENV/Scripts/cli/main.py
```

## Credits

Adapted from the ProcurementAgentAI CLI system, which uses Click for command-line interface construction.
