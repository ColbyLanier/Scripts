"""Configuration management for CLI tool."""

import os
from dataclasses import dataclass
from typing import Dict, Optional

import click


@dataclass
class EnvironmentConfig:
    """Configuration for a specific environment."""

    name: str
    description: str
    # Add additional environment-specific config here as needed


# Environment configurations
ENVIRONMENTS: Dict[str, EnvironmentConfig] = {
    "default": EnvironmentConfig(
        name="default",
        description="Default environment configuration",
    ),
    # Add more environments as needed
}


class CLIConfig:
    """CLI configuration manager."""

    def __init__(self):
        self.environment = os.environ.get("OBSIDIAN_CLI_ENV", "default")
        self.verbose = os.environ.get("OBSIDIAN_CLI_VERBOSE", "false").lower() == "true"

    @property
    def env_config(self) -> EnvironmentConfig:
        """Get configuration for current environment."""
        return ENVIRONMENTS.get(self.environment, ENVIRONMENTS["default"])

    def validate(self) -> None:
        """Validate configuration."""
        if self.environment not in ENVIRONMENTS:
            valid_envs = ", ".join(ENVIRONMENTS.keys())
            raise click.ClickException(
                f"Invalid environment '{self.environment}'. "
                f"Valid options: {valid_envs}"
            )


def get_config() -> CLIConfig:
    """Get CLI configuration instance."""
    config = CLIConfig()
    config.validate()
    return config


def environment_option(f):
    """Decorator to add environment option to commands."""
    env_choices = list(ENVIRONMENTS.keys())
    env_choices.sort()

    return click.option(
        "--env",
        type=click.Choice(env_choices),
        default="default",
        help="Target environment",
        show_default=True,
    )(f)


def verbose_option(f):
    """Decorator to add verbose option to commands."""
    return click.option(
        "--verbose", "-v",
        is_flag=True,
        help="Enable verbose output",
    )(f)
