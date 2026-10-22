"""RSA key pairs for ActivityPub actors. A module of its own, importing nothing of the app's, so that app.models can
make a domain's instance actor key without importing app.activitypub.signature, which imports app.models."""
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


class RsaKeys:
    @classmethod
    def generate_keypair(cls) -> tuple[str, str]:
        """
        Generates a new RSA keypair
        """
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        private_key_serialized = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")
        public_key_serialized = (
            private_key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("ascii")
        )
        return private_key_serialized, public_key_serialized
