"""Relay CLI commands (spec: Admin and CLI). Registered from app/cli.py."""
import sys

import click

from app import db
from app.models import Relay
from app.relays.subscribe import RelayError, add_relay, remove_relay, retry_relay


def register_relay_commands(app) -> None:
    @app.cli.group('relays')
    def relays():
        """Subscribe this instance to ActivityPub relays."""

    @relays.command('add')
    @click.argument('url')
    def add(url):
        try:
            relay = add_relay(url)
        except RelayError as error:
            click.echo(str(error))
            sys.exit(1)
        click.echo(f'{relay.url}: {relay.style}, {relay.state}')

    def _row(url):
        relay = db.session.query(Relay).filter_by(url=url.strip()).first()
        if relay is None:
            click.echo('no such relay')
            sys.exit(1)
        return relay

    @relays.command('remove')
    @click.argument('url')
    def remove(url):
        remove_relay(_row(url))
        click.echo('removed')

    @relays.command('retry')
    @click.argument('url')
    def retry(url):
        relay = _row(url)
        try:
            retry_relay(relay)
        except RelayError as error:
            click.echo(str(error))
            sys.exit(1)
        click.echo(f'{relay.url}: {relay.state}')

    @relays.command('list')
    def list_relays():
        for relay in db.session.query(Relay).order_by(Relay.created_at):
            click.echo(f'{relay.url}\t{relay.style}\t{relay.state}\t{relay.last_error or ""}')
