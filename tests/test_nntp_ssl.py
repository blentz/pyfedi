"""The NNTP server's TLS (app/nntp/nntpserver.py, use_ssl): one SSLContext built at start-up from the certificate and
key, wrapping every accepted connection. It used ssl.wrap_socket, which Python 3.12 removed, so every TLS connection
raised AttributeError."""
import datetime
import socket
import ssl
import threading
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import app.nntp.nntpserver as nntpserver
from app.nntp.server import PieFedNNTPConnectionHandler, PieFedNNTPServer


@pytest.fixture
def certificate(tmp_path):
    """(certfile, keyfile) of a self-signed certificate for localhost."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    certfile, keyfile = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    return str(certfile), str(keyfile)


def _server(**kwargs):
    return PieFedNNTPServer(SimpleNamespace(config={}), ('127.0.0.1', 0), PieFedNNTPConnectionHandler, **kwargs)


def test_a_tls_server_hands_out_tls_connections(certificate):
    certfile, keyfile = certificate
    server = _server(use_ssl=True, certfile=certfile, keyfile=keyfile)
    try:
        assert isinstance(server.ssl_context, ssl.SSLContext)
        client_context = ssl.create_default_context(cafile=certfile)
        received = []

        def client():
            with socket.create_connection(server.server_address, timeout=5) as raw:
                with client_context.wrap_socket(raw, server_hostname='localhost') as tls:
                    tls.sendall(b'MODE READER\r\n')

        thread = threading.Thread(target=client)
        thread.start()
        connection, _address = server.get_request()
        with connection:
            assert isinstance(connection, ssl.SSLSocket)
            received.append(connection.recv(64))
        thread.join(timeout=5)
        assert received == [b'MODE READER\r\n']
    finally:
        server.server_close()


def test_a_plain_server_has_no_context_and_hands_out_plain_connections():
    server = _server()
    try:
        assert server.ssl_context is None
        thread = threading.Thread(target=lambda: socket.create_connection(server.server_address, timeout=5).close())
        thread.start()
        connection, _address = server.get_request()
        with connection:
            assert not isinstance(connection, ssl.SSLSocket)
        thread.join(timeout=5)
    finally:
        server.server_close()


def test_a_missing_certificate_file_fails_at_start_up(certificate, tmp_path):
    _certfile, keyfile = certificate
    with pytest.raises(FileNotFoundError):
        _server(use_ssl=True, certfile=str(tmp_path / 'absent.pem'), keyfile=keyfile)


@pytest.mark.parametrize('missing', ['certfile', 'keyfile'])
def test_tls_without_a_certificate_or_key_is_refused(certificate, missing):
    files = dict(zip(('certfile', 'keyfile'), certificate))
    files[missing] = None
    with pytest.raises(ValueError, match='certfile and keyfile'):
        _server(use_ssl=True, **files)


def test_tls_without_the_ssl_module_is_refused(certificate, monkeypatch):
    certfile, keyfile = certificate
    monkeypatch.setattr(nntpserver, '_have_ssl', False)
    with pytest.raises(ValueError, match='ssl module could not be imported'):
        _server(use_ssl=True, certfile=certfile, keyfile=keyfile)
