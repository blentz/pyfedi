"""ATProto: the protocol layer Bluesky's two account kinds share (spec 2026-11-23-atproto-federation-design.md §4).

A member reaches the network through a Session: their own account, connected by OAuth
(app.connections.bluesky), or an account PieFed hosts for them on its own PDS. Everything built on a session (what a
post becomes, the identity's records, notifications) works the same whichever kind it is given.

- resolve.py: a handle to its DID, a DID to its document, a document to its PDS.
- records.py: the records a post becomes (a full post, or a card linking back) and their facets and labels.
"""


class Session:
    """One account's way to call its PDS. `did` is the account, `pds` its server's base address, and `xrpc` sends
    one call there with the account's own authorization, answering the checked response (app.connections.http.check)
    or raising its ServiceError."""
    did: str = ''
    pds: str = ''

    def xrpc(self, method: str, nsid: str, **kwargs):
        raise NotImplementedError
