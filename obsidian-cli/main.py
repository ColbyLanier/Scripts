#!/usr/bin/env python3
"""
Obsidian CLI Framework

A modular command-line interface for custom bash commands.
"""

import os
import sys
from pathlib import Path

import click

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from Scripts.cli.config import ENVIRONMENTS, environment_option, get_config, verbose_option


def _normalize_cli_env(env: str) -> str:
    """Normalize environment for CLI use."""
    return env.lower()


@click.group()
@environment_option
@verbose_option
@click.pass_context
def cli(ctx, env, verbose):
    """Obsidian CLI - Modular command framework for custom bash commands."""
    # Set environment variables for the session
    normalized_env = _normalize_cli_env(env)
    os.environ["OBSIDIAN_CLI_ENV"] = normalized_env
    if verbose:
        os.environ["OBSIDIAN_CLI_VERBOSE"] = "true"

    # Store config in context for subcommands
    config = get_config()
    ctx.ensure_object(dict)
    ctx.obj["config"] = config

    if verbose:
        click.echo(f"Using environment: {config.env_config.name}")


@cli.command()
@click.pass_context
def info(ctx):
    """Show CLI information."""
    config = ctx.obj["config"]

    click.echo("Obsidian CLI Framework")
    click.echo("=" * 50)
    click.echo(f"CLI Version: 1.0.0")
    click.echo(f"Environment: {config.env_config.name}")
    click.echo(f"Description: {config.env_config.description}")
    click.echo()

    click.echo("Available Environments:")
    for env_name, env_config in ENVIRONMENTS.items():
        marker = "→" if env_name == config.environment else " "
        click.echo(f"  {marker} {env_name}: {env_config.description}")


# Command group registration
# Import and add your command groups here
from Scripts.cli.commands.example import example_commands
from Scripts.cli.commands.obsidian import obsidian_commands

cli.add_command(example_commands, name="example")
cli.add_command(obsidian_commands, name="obs")


if __name__ == "__main__":
    cli()
