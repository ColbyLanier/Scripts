#!/usr/bin/env python3
"""
Setup script for Obsidian CLI Framework.

This script makes the CLI accessible as a command-line tool.
"""

import os
import subprocess
import sys
from pathlib import Path


def main():
    """Setup the CLI tool."""
    # Get the project root (Token-ENV)
    project_root = Path(__file__).parent.parent.parent.absolute()
    cli_main = project_root / "Scripts" / "cli" / "main.py"

    # Make the CLI script executable
    cli_main.chmod(0o755)

    # Create a simple wrapper script
    wrapper_script = f"""#!/usr/bin/env python3
import sys
import os

# Add project root to Python path
project_root = "{project_root}"
sys.path.insert(0, project_root)

# Import and run CLI
from Scripts.cli.main import cli

if __name__ == "__main__":
    cli()
"""

    # Write wrapper script
    wrapper_path = project_root / "obscli"
    with open(wrapper_path, "w") as f:
        f.write(wrapper_script)

    wrapper_path.chmod(0o755)

    print("✓ CLI setup complete!")
    print(f"✓ CLI wrapper created at: {wrapper_path}")
    print()
    print("To use the CLI:")
    print(f"1. Add to PATH: export PATH=\"{project_root}:$PATH\"")
    print(f"2. Or add alias: alias obscli=\"{wrapper_path}\"")
    print(f"3. Or run directly: {wrapper_path}")
    print()
    print("Examples:")
    print("  obscli --help")
    print("  obscli info")
    print("  obscli example hello World")
    print("  obscli example echo --count 3 'Hello!'")
    print()
    print("To add custom commands:")
    print(f"1. Create command file in: {project_root}/Scripts/cli/commands/")
    print("2. Follow the pattern in example.py")
    print("3. Register in main.py")


if __name__ == "__main__":
    main()
