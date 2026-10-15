"""ALLOWED_PRIVATE_NETWORKS (spec 2026-10-14 §10 A5): the interop harness's containers talk over a podman network
of private addresses, which the SSRF guard refuses. An operator-listed network is admitted; nothing else changes."""
import ipaddress

import pytest

from app import pinned_http
from app.pinned_http import is_refused_address, parse_networks


def test_parse_networks_reads_a_comma_separated_list():
    assert parse_networks('10.89.0.0/16, fd00::/8') == (ipaddress.ip_network('10.89.0.0/16'),
                                                         ipaddress.ip_network('fd00::/8'))


def test_parse_networks_of_empty_is_empty():
    assert parse_networks('') == ()
    assert parse_networks(' , ') == ()


def test_parse_networks_refuses_garbage():
    with pytest.raises(ValueError):
        parse_networks('not-a-network')


def test_private_address_refused_by_default(monkeypatch):
    monkeypatch.setattr(pinned_http, 'ALLOWED_PRIVATE_NETWORKS', ())
    assert is_refused_address(ipaddress.ip_address('10.89.0.5'))


def test_listed_network_is_admitted(monkeypatch):
    monkeypatch.setattr(pinned_http, 'ALLOWED_PRIVATE_NETWORKS', parse_networks('10.89.0.0/16'))
    assert not is_refused_address(ipaddress.ip_address('10.89.0.5'))


def test_address_outside_listed_network_still_refused(monkeypatch):
    monkeypatch.setattr(pinned_http, 'ALLOWED_PRIVATE_NETWORKS', parse_networks('10.89.0.0/16'))
    assert is_refused_address(ipaddress.ip_address('10.90.0.5'))
    assert is_refused_address(ipaddress.ip_address('127.0.0.1'))


def test_ipv4_address_against_ipv6_network_is_not_admitted(monkeypatch):
    monkeypatch.setattr(pinned_http, 'ALLOWED_PRIVATE_NETWORKS', parse_networks('fd00::/8'))
    assert is_refused_address(ipaddress.ip_address('10.89.0.5'))


def test_global_address_unaffected(monkeypatch):
    monkeypatch.setattr(pinned_http, 'ALLOWED_PRIVATE_NETWORKS', parse_networks('10.89.0.0/16'))
    assert not is_refused_address(ipaddress.ip_address('8.8.8.8'))
