"""Discovery CLI commands (interop D24). Registered from app/cli.py's register()."""
import click

from app.discovery.refresh import refresh_discovery


def register_discovery_commands(app) -> None:
    @app.cli.command('refresh_discovery')
    def refresh_discovery_command():
        """Refresh the discovery directory from SepiaSearch, Podcast Index, Mastodon and Pixelfed directories."""
        for source, outcome in refresh_discovery().items():
            click.echo(f'{source}: {outcome}')
