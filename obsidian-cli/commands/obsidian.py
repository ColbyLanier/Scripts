"""
Obsidian Advanced URI command builder.

Execute Obsidian commands from the command line using Advanced URI plugin.
"""

import click
import subprocess
import urllib.parse
from pathlib import Path


class ObsidianURI:
    """Build and execute Obsidian Advanced URIs."""

    def __init__(self, vault="Token-ENV"):
        self.vault = vault
        self.base_uri = "obsidian://adv-uri"
        self.params = {}

    def add_param(self, key, value):
        """Add a parameter to the URI."""
        if value is not None:
            self.params[key] = str(value)
        return self

    def build(self):
        """Build the complete URI string."""
        uri = f"{self.base_uri}?vault={urllib.parse.quote(self.vault)}"

        for key, value in self.params.items():
            uri += f"&{key}={urllib.parse.quote(value)}"

        return uri

    def execute(self, silent=False):
        """Execute the URI using PowerShell."""
        uri = self.build()

        if not silent:
            click.echo(f"Executing: {uri}")

        try:
            # Use PowerShell to open URI in hidden window
            cmd = f'powershell.exe -Command "Start-Process -WindowStyle Hidden \'{uri}\'"'
            subprocess.run(cmd, shell=True, check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError as e:
            click.echo(f"Error executing URI: {e}", err=True)
            return False


@click.group()
@click.option("--vault", default="Token-ENV", help="Obsidian vault name")
@click.pass_context
def obsidian_commands(ctx, vault):
    """Obsidian Advanced URI commands - Execute Obsidian actions from CLI."""
    ctx.ensure_object(dict)
    ctx.obj["vault"] = vault


@obsidian_commands.command()
@click.argument("filepath")
@click.option("--heading", help="Navigate to specific heading")
@click.option("--block", help="Navigate to specific block ID")
@click.option("--line", type=int, help="Navigate to specific line number")
@click.option("--mode", type=click.Choice(["source", "preview", "live"]), help="View mode")
@click.option("--viewmode", help="Alternative: source/preview/live")
@click.pass_context
def open(ctx, filepath, heading, block, line, mode, viewmode):
    """Open a file in Obsidian."""
    uri = ObsidianURI(ctx.obj["vault"])
    uri.add_param("filepath", filepath)

    if heading:
        uri.add_param("heading", heading)
    if block:
        uri.add_param("block", block)
    if line:
        uri.add_param("line", line)
    if mode:
        uri.add_param("mode", mode)
    elif viewmode:
        uri.add_param("viewmode", viewmode)

    if uri.execute():
        click.echo(f"✓ Opened: {filepath}")


@obsidian_commands.command()
@click.argument("filepath")
@click.argument("content")
@click.option("--mode", type=click.Choice(["append", "prepend", "overwrite"]), default="append")
@click.option("--heading", help="Insert under specific heading")
@click.option("--block", help="Insert after specific block")
@click.option("--line", type=int, help="Insert at specific line")
@click.pass_context
def write(ctx, filepath, content, mode, heading, block, line):
    """Write content to a file."""
    uri = ObsidianURI(ctx.obj["vault"])
    uri.add_param("filepath", filepath)
    uri.add_param("data", content)

    if mode == "append":
        uri.add_param("mode", "append")
    elif mode == "prepend":
        uri.add_param("mode", "prepend")
    elif mode == "overwrite":
        uri.add_param("mode", "overwrite")

    if heading:
        uri.add_param("heading", heading)
    if block:
        uri.add_param("block", block)
    if line:
        uri.add_param("line", line)

    if uri.execute():
        click.echo(f"✓ Written to: {filepath}")


@obsidian_commands.command()
@click.argument("workspace_name")
@click.pass_context
def workspace(ctx, workspace_name):
    """Load a specific workspace."""
    uri = ObsidianURI(ctx.obj["vault"])
    uri.add_param("workspace", workspace_name)

    if uri.execute():
        click.echo(f"✓ Loaded workspace: {workspace_name}")


@obsidian_commands.command()
@click.argument("command_id")
@click.option("--file", help="Open file before executing command")
@click.pass_context
def command(ctx, command_id, file):
    """Execute an Obsidian command by ID."""
    uri = ObsidianURI(ctx.obj["vault"])
    uri.add_param("commandid", command_id)

    if file:
        uri.add_param("filepath", file)

    if uri.execute():
        click.echo(f"✓ Executed command: {command_id}")


@obsidian_commands.command()
@click.argument("query")
@click.option("--replace", help="Replace matches with this text")
@click.option("--file", help="Limit search to specific file")
@click.pass_context
def search(ctx, query, replace, file):
    """Search (and optionally replace) text."""
    uri = ObsidianURI(ctx.obj["vault"])
    uri.add_param("search", query)

    if replace:
        uri.add_param("replace", replace)
    if file:
        uri.add_param("filepath", file)

    if uri.execute():
        if replace:
            click.echo(f"✓ Replaced '{query}' with '{replace}'")
        else:
            click.echo(f"✓ Searched for: {query}")


@obsidian_commands.command()
@click.pass_context
def daily(ctx):
    """Open today's daily note."""
    uri = ObsidianURI(ctx.obj["vault"])
    uri.add_param("daily", "true")

    if uri.execute():
        click.echo("✓ Opened daily note")


@obsidian_commands.command()
@click.argument("setting_id")
@click.pass_context
def settings(ctx, setting_id):
    """Open specific settings panel."""
    uri = ObsidianURI(ctx.obj["vault"])
    uri.add_param("settingid", setting_id)

    if uri.execute():
        click.echo(f"✓ Opened settings: {setting_id}")


@obsidian_commands.command()
@click.argument("filepath")
@click.argument("key")
@click.argument("value")
@click.pass_context
def frontmatter(ctx, filepath, key, value):
    """Set frontmatter key-value pair."""
    uri = ObsidianURI(ctx.obj["vault"])
    uri.add_param("filepath", filepath)
    uri.add_param("frontmatterkey", key)
    uri.add_param("data", value)

    if uri.execute():
        click.echo(f"✓ Set {key}={value} in {filepath}")


@obsidian_commands.command()
@click.argument("javascript_code")
@click.pass_context
def eval(ctx, javascript_code):
    """Execute JavaScript code in Obsidian context."""
    uri = ObsidianURI(ctx.obj["vault"])
    uri.add_param("eval", javascript_code)

    if uri.execute():
        click.echo("✓ Executed JavaScript")


@obsidian_commands.command()
@click.argument("filepath")
@click.option("--heading", help="Heading")
@click.option("--block", help="Block ID")
@click.option("--line", type=int, help="Line number")
@click.option("--mode", type=click.Choice(["source", "preview"]), help="View mode")
@click.pass_context
def uri(ctx, filepath, heading, block, line, mode):
    """Build and display URI without executing."""
    uri_builder = ObsidianURI(ctx.obj["vault"])
    uri_builder.add_param("filepath", filepath)

    if heading:
        uri_builder.add_param("heading", heading)
    if block:
        uri_builder.add_param("block", block)
    if line:
        uri_builder.add_param("line", line)
    if mode:
        uri_builder.add_param("mode", mode)

    click.echo(uri_builder.build())


# Convenience shortcuts for common commands
@obsidian_commands.group()
def quick():
    """Quick shortcuts for common Obsidian commands."""
    pass


@quick.command()
@click.pass_context
def delete(ctx):
    """Delete current file."""
    uri = ObsidianURI(ctx.obj["vault"])
    uri.add_param("commandid", "app:delete-file")

    if uri.execute():
        click.echo("✓ Delete command executed")


@quick.command()
@click.pass_context
def reload(ctx):
    """Reload Obsidian."""
    uri = ObsidianURI(ctx.obj["vault"])
    uri.add_param("commandid", "app:reload")

    if uri.execute():
        click.echo("✓ Reload command executed")


@quick.command()
@click.pass_context
def toggle_mode(ctx):
    """Toggle reading/editing mode."""
    uri = ObsidianURI(ctx.obj["vault"])
    uri.add_param("commandid", "markdown:toggle-preview")

    if uri.execute():
        click.echo("✓ Toggled view mode")


@quick.command()
@click.argument("workspace_num", type=int)
@click.pass_context
def ws(ctx, workspace_num):
    """Load workspace by number (0-6)."""
    workspace_map = {
        0: "0-Admin",
        1: "1-Obsidian",
        2: "2-Civic",
        3: "3-Algorithms",
        4: "4-Computing",
        5: "5-Scheming",
        6: "6-Scheming"
    }

    workspace_name = workspace_map.get(workspace_num)
    if not workspace_name:
        click.echo(f"✗ Invalid workspace number: {workspace_num}")
        return

    uri = ObsidianURI(ctx.obj["vault"])
    uri.add_param("workspace", workspace_name)

    if uri.execute():
        click.echo(f"✓ Loaded workspace: {workspace_name}")


@quick.command()
@click.argument("note_name")
@click.pass_context
def create(ctx, note_name):
    """Create a new note."""
    uri = ObsidianURI(ctx.obj["vault"])
    uri.add_param("filepath", f"{note_name}.md")
    uri.add_param("data", f"# {note_name}\n\n")

    if uri.execute():
        click.echo(f"✓ Created note: {note_name}")
