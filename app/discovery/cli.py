"""Discovery CLI commands (interop D24). Registered from app/cli.py's register()."""
import click
from flask import current_app

from app.discovery.refresh import refresh_discovery
from app.discovery.seed import seed_from_fixtures
from app.discovery.sync import enqueue_polls, reconcile_sync


def register_discovery_commands(app) -> None:
    @app.cli.command('refresh_discovery')
    def refresh_discovery_command():
        """Refresh the discovery directory from SepiaSearch, index.castopod.org, Mastodon and Pixelfed directories."""
        for source, outcome in refresh_discovery().items():
            click.echo(f'{source}: {outcome}')

    @app.cli.command('sync_discovery')
    def sync_discovery_command():
        """Keep each host's top PeerTube channels and Castopod podcasts followed by the instance actor, then queue
        their daily polls. Run after refresh_discovery."""
        summary = reconcile_sync()
        click.echo(f"added: {summary['added']}")
        click.echo(f"dropped: {summary['dropped']}")
        click.echo(f"refollowed: {summary['refollowed']}")
        click.echo(f"failed hosts: {', '.join(summary['failed_hosts']) or 'none'}")
        click.echo(f'polls queued: {enqueue_polls()}')

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
