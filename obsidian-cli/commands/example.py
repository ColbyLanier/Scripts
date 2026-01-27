"""
Example command group.

This demonstrates how to create a command group for the CLI.
"""

import click


@click.group()
@click.pass_context
def example_commands(ctx):
    """Example commands - demonstrates command group pattern."""
    pass


@example_commands.command()
@click.argument("name")
@click.pass_context
def hello(ctx, name):
    """Say hello to someone."""
    config = ctx.obj["config"]

    if config.verbose:
        click.echo(f"Running in {config.env_config.name} environment")

    click.echo(f"Hello, {name}!")


@example_commands.command()
@click.option("--count", default=1, help="Number of times to repeat")
@click.argument("message")
@click.pass_context
def echo(ctx, count, message):
    """Echo a message multiple times."""
    for _ in range(count):
        click.echo(message)
