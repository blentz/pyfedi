"""Discovery CLI commands (interop D24). Registered from app/cli.py's register()."""
import click
from flask import current_app

from app.discovery.refresh import refresh_discovery
from app.discovery.seed import seed_from_fixtures


def register_discovery_commands(app) -> None:
    @app.cli.command('refresh_discovery')
    def refresh_discovery_command():
        """Refresh the discovery directory from SepiaSearch, Podcast Index, Mastodon and Pixelfed directories."""
        for source, outcome in refresh_discovery().items():
            click.echo(f'{source}: {outcome}')

    @app.cli.command('discovery-seed-fixtures')
    @click.option('--force', is_flag=True, help='Seed even though the app is not in debug mode.')
    def discovery_seed_fixtures_command(force):
        """Load the discovery fixture files into this database for local validation. Fetches nothing.

        Creates the discovery entries, the podcast user and its podcast community, one episode post with credits,
        and the credited remote user (ann@people.example) whose profile vouches for the podcast."""
        if not current_app.debug and not force:
            raise click.ClickException('Refusing: this writes fixture rows. Run with FLASK_DEBUG=1, or pass --force.')
        result = seed_from_fixtures()
        for created, row in result.items():
            click.echo(f'{created}: {row}')
