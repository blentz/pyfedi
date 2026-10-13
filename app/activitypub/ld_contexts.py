"""Bundled copies of the JSON-LD contexts LD signatures normalize against.

`LDSignature.normalized_hash` runs `jsonld.normalize`, which resolves every
`@context` URL through pyld's document loader. pyld's default loader fetches
each one over the network on every call, with no cache and no timeout, so an
inbox LD-signature check failed whenever w3id.org or w3.org was slow or down.
It was also SSRF: the sender chooses @context, so any actor could make this
server GET an arbitrary URL before its signature was even checked. These
documents are effectively frozen, so they are served from memory, and any
other context is refused rather than fetched.

The two documents were fetched once from their real URLs and pasted in
as-received, not hand-written, so URDNA2015 normalization sees exactly what
the network would serve.
"""
import json
import re

from pyld import jsonld

ACTIVITYSTREAMS_CONTEXT = json.loads(
    '{"@context":{"@vocab":"_:","xsd":"http://www.w3.org/2001/XMLSchema#","as":"https://www.w3.org/ns/activitystreams#","ldp":"http://www.w3.org/ns/ldp#","vcard":"http://www.w3.org/2006/vcard/ns#","id":"@id","type":"@type","Accept":"as:Accept","Activity":"as:Activity","IntransitiveActivity":"as:IntransitiveActivity","Add":"as:Add","Announce":"as:Announce","Application":"as:Application","Arrive":"as:Arrive","Article":"as:Article","Audio":"as:Audio","Block":"as:Block","Collection":"as:Collection","CollectionPage":"as:CollectionPage","Relationship":"as:Relationship","Create":"as:Create","Delete":"as:Delete","Dislike":"as:Dislike","Document":"as:Document","Event":"as:Event","Follow":"as:Follow","Flag":"as:Flag","Group":"as:Group","Ignore":"as:Ignore","Image":"as:Image","Invite":"as:Invite","Join":"as:Join","Leave":"as:Leave","Like":"as:Like","Link":"as:Link","Mention":"as:Mention","Note":"as:Note","Object":"as:Object","Offer":"as:Offer","OrderedCollection":"as:OrderedCollection","OrderedCollectionPage":"as:OrderedCollectionPage","Organization":"as:Organization","Page":"as:Page","Person":"as:Person","Place":"as:Place","Profile":"as:Profile","Question":"as:Question","Reject":"as:Reject","Remove":"as:Remove","Service":"as:Service","TentativeAccept":"as:TentativeAccept","TentativeReject":"as:TentativeReject","Tombstone":"as:Tombstone","Undo":"as:Undo","Update":"as:Update","Video":"as:Video","View":"as:View","Listen":"as:Listen","Read":"as:Read","Move":"as:Move","Travel":"as:Travel","IsFollowing":"as:IsFollowing","IsFollowedBy":"as:IsFollowedBy","IsContact":"as:IsContact","IsMember":"as:IsMember","subject":{"@id":"as:subject","@type":"@id"},"relationship":{"@id":"as:relationship","@type":"@id"},"actor":{"@id":"as:actor","@type":"@id"},"attributedTo":{"@id":"as:attributedTo","@type":"@id"},"attachment":{"@id":"as:attachment","@type":"@id"},"bcc":{"@id":"as:bcc","@type":"@id"},"bto":{"@id":"as:bto","@type":"@id"},"cc":{"@id":"as:cc","@type":"@id"},"context":{"@id":"as:context","@type":"@id"},"current":{"@id":"as:current","@type":"@id"},"first":{"@id":"as:first","@type":"@id"},"generator":{"@id":"as:generator","@type":"@id"},"icon":{"@id":"as:icon","@type":"@id"},"image":{"@id":"as:image","@type":"@id"},"inReplyTo":{"@id":"as:inReplyTo","@type":"@id"},"items":{"@id":"as:items","@type":"@id"},"instrument":{"@id":"as:instrument","@type":"@id"},"orderedItems":{"@id":"as:items","@type":"@id","@container":"@list"},"last":{"@id":"as:last","@type":"@id"},"location":{"@id":"as:location","@type":"@id"},"next":{"@id":"as:next","@type":"@id"},"object":{"@id":"as:object","@type":"@id"},"oneOf":{"@id":"as:oneOf","@type":"@id"},"anyOf":{"@id":"as:anyOf","@type":"@id"},"closed":{"@id":"as:closed","@type":"xsd:dateTime"},"origin":{"@id":"as:origin","@type":"@id"},"accuracy":{"@id":"as:accuracy","@type":"xsd:float"},"prev":{"@id":"as:prev","@type":"@id"},"preview":{"@id":"as:preview","@type":"@id"},"replies":{"@id":"as:replies","@type":"@id"},"result":{"@id":"as:result","@type":"@id"},"audience":{"@id":"as:audience","@type":"@id"},"partOf":{"@id":"as:partOf","@type":"@id"},"tag":{"@id":"as:tag","@type":"@id"},"target":{"@id":"as:target","@type":"@id"},"to":{"@id":"as:to","@type":"@id"},"url":{"@id":"as:url","@type":"@id"},"altitude":{"@id":"as:altitude","@type":"xsd:float"},"content":"as:content","contentMap":{"@id":"as:content","@container":"@language"},"name":"as:name","nameMap":{"@id":"as:name","@container":"@language"},"duration":{"@id":"as:duration","@type":"xsd:duration"},"endTime":{"@id":"as:endTime","@type":"xsd:dateTime"},"height":{"@id":"as:height","@type":"xsd:nonNegativeInteger"},"href":{"@id":"as:href","@type":"@id"},"hreflang":"as:hreflang","latitude":{"@id":"as:latitude","@type":"xsd:float"},"longitude":{"@id":"as:longitude","@type":"xsd:float"},"mediaType":"as:mediaType","published":{"@id":"as:published","@type":"xsd:dateTime"},"radius":{"@id":"as:radius","@type":"xsd:float"},"rel":"as:rel","startIndex":{"@id":"as:startIndex","@type":"xsd:nonNegativeInteger"},"startTime":{"@id":"as:startTime","@type":"xsd:dateTime"},"summary":"as:summary","summaryMap":{"@id":"as:summary","@container":"@language"},"totalItems":{"@id":"as:totalItems","@type":"xsd:nonNegativeInteger"},"units":"as:units","updated":{"@id":"as:updated","@type":"xsd:dateTime"},"width":{"@id":"as:width","@type":"xsd:nonNegativeInteger"},"describes":{"@id":"as:describes","@type":"@id"},"formerType":{"@id":"as:formerType","@type":"@id"},"deleted":{"@id":"as:deleted","@type":"xsd:dateTime"},"inbox":{"@id":"ldp:inbox","@type":"@id"},"outbox":{"@id":"as:outbox","@type":"@id"},"following":{"@id":"as:following","@type":"@id"},"followers":{"@id":"as:followers","@type":"@id"},"streams":{"@id":"as:streams","@type":"@id"},"preferredUsername":"as:preferredUsername","endpoints":{"@id":"as:endpoints","@type":"@id"},"uploadMedia":{"@id":"as:uploadMedia","@type":"@id"},"proxyUrl":{"@id":"as:proxyUrl","@type":"@id"},"liked":{"@id":"as:liked","@type":"@id"},"oauthAuthorizationEndpoint":{"@id":"as:oauthAuthorizationEndpoint","@type":"@id"},"oauthTokenEndpoint":{"@id":"as:oauthTokenEndpoint","@type":"@id"},"provideClientKey":{"@id":"as:provideClientKey","@type":"@id"},"signClientKey":{"@id":"as:signClientKey","@type":"@id"},"sharedInbox":{"@id":"as:sharedInbox","@type":"@id"},"Public":{"@id":"as:Public","@type":"@id"},"source":"as:source","likes":{"@id":"as:likes","@type":"@id"},"shares":{"@id":"as:shares","@type":"@id"},"alsoKnownAs":{"@id":"as:alsoKnownAs","@type":"@id"}}}'
)
SECURITY_V1_CONTEXT = json.loads(
    '{"@context":{"id":"@id","type":"@type","dc":"http://purl.org/dc/terms/","sec":"https://w3id.org/security#","xsd":"http://www.w3.org/2001/XMLSchema#","EcdsaKoblitzSignature2016":"sec:EcdsaKoblitzSignature2016","Ed25519Signature2018":"sec:Ed25519Signature2018","EncryptedMessage":"sec:EncryptedMessage","GraphSignature2012":"sec:GraphSignature2012","LinkedDataSignature2015":"sec:LinkedDataSignature2015","LinkedDataSignature2016":"sec:LinkedDataSignature2016","CryptographicKey":"sec:Key","authenticationTag":"sec:authenticationTag","canonicalizationAlgorithm":"sec:canonicalizationAlgorithm","cipherAlgorithm":"sec:cipherAlgorithm","cipherData":"sec:cipherData","cipherKey":"sec:cipherKey","created":{"@id":"dc:created","@type":"xsd:dateTime"},"creator":{"@id":"dc:creator","@type":"@id"},"digestAlgorithm":"sec:digestAlgorithm","digestValue":"sec:digestValue","domain":"sec:domain","encryptionKey":"sec:encryptionKey","expiration":{"@id":"sec:expiration","@type":"xsd:dateTime"},"expires":{"@id":"sec:expiration","@type":"xsd:dateTime"},"initializationVector":"sec:initializationVector","iterationCount":"sec:iterationCount","nonce":"sec:nonce","normalizationAlgorithm":"sec:normalizationAlgorithm","owner":{"@id":"sec:owner","@type":"@id"},"password":"sec:password","privateKey":{"@id":"sec:privateKey","@type":"@id"},"privateKeyPem":"sec:privateKeyPem","publicKey":{"@id":"sec:publicKey","@type":"@id"},"publicKeyBase58":"sec:publicKeyBase58","publicKeyPem":"sec:publicKeyPem","publicKeyWif":"sec:publicKeyWif","publicKeyService":{"@id":"sec:publicKeyService","@type":"@id"},"revoked":{"@id":"sec:revoked","@type":"xsd:dateTime"},"salt":"sec:salt","signature":"sec:signature","signatureAlgorithm":"sec:signingAlgorithm","signatureValue":"sec:signatureValue"}}'
)
BUNDLED_LD_CONTEXTS = {
    'https://www.w3.org/ns/activitystreams': ACTIVITYSTREAMS_CONTEXT,
    'https://w3id.org/security/v1': SECURITY_V1_CONTEXT,
}

def bundled_document(url: str) -> dict:
    """The pyld remote-document shape for a bundled context."""
    return {'contentType': 'application/ld+json', 'contextUrl': None,
            'documentUrl': url, 'document': BUNDLED_LD_CONTEXTS[url]}


def ld_document_loader(url, options=None):
    """pyld document loader: bundled contexts from memory, never the network."""
    if url not in BUNDLED_LD_CONTEXTS:
        raise jsonld.JsonLdError(f'JSON-LD context {url} is not bundled, and is not fetched',
                                 'jsonld.LoadDocumentError', {'url': url},
                                 code='loading document failed')
    return bundled_document(url)


# ---------------------------------------------------------------------------
# What an LD signature covers, and the gate that refuses a document whose JSON
# says more than its signed graph does.
# ---------------------------------------------------------------------------

# The extension terms pyfedi itself declares in its outgoing @context (the same
# map Lemmy declares). `default_context` sends exactly this.
PYFEDI_CONTEXT_TERMS = {
    "lemmy": "https://join-lemmy.org/ns#",
    "litepub": "http://litepub.social/ns#",
    "pt": "https://joinpeertube.org/ns#",
    "sc": "http://schema.org/",
    "ChatMessage": "litepub:ChatMessage",
    "commentsEnabled": "pt:commentsEnabled",
    "sensitive": "as:sensitive",
    "matrixUserId": "lemmy:matrixUserId",
    "postingRestrictedToMods": "lemmy:postingRestrictedToMods",
    "removeData": "lemmy:removeData",
    "stickied": "lemmy:stickied",
    "moderators": {
        "@type": "@id",
        "@id": "lemmy:moderators"
    },
    "expires": "as:endTime",
    "distinguished": "lemmy:distinguished",
    "language": "sc:inLanguage",
    "identifier": "sc:identifier"
}

# Mastodon's inline term map, copied from CONTEXT_EXTENSION_MAP in its
# app/helpers/context_helper.rb (main, 2026-10). A Mastodon document carries the
# subset of it that its object uses.
MASTODON_CONTEXT_TERMS = {
    "toot": "http://joinmastodon.org/ns#",
    "schema": "http://schema.org#",
    "ostatus": "http://ostatus.org#",
    "gts": "https://gotosocial.org/ns#",
    "manuallyApprovesFollowers": "as:manuallyApprovesFollowers",
    "sensitive": "as:sensitive",
    "Hashtag": "as:Hashtag",
    "movedTo": {"@id": "as:movedTo", "@type": "@id"},
    "alsoKnownAs": {"@id": "as:alsoKnownAs", "@type": "@id"},
    "Emoji": "toot:Emoji",
    "featured": {"@id": "toot:featured", "@type": "@id"},
    "featuredTags": {"@id": "toot:featuredTags", "@type": "@id"},
    "PropertyValue": "schema:PropertyValue",
    "value": "schema:value",
    "atomUri": "ostatus:atomUri",
    "inReplyToAtomUri": "ostatus:inReplyToAtomUri",
    "conversation": "ostatus:conversation",
    "focalPoint": {"@container": "@list", "@id": "toot:focalPoint"},
    "blurhash": "toot:blurhash",
    "discoverable": "toot:discoverable",
    "indexable": "toot:indexable",
    "memorial": "toot:memorial",
    "votersCount": "toot:votersCount",
    "suspended": "toot:suspended",
    "attributionDomains": {"@id": "toot:attributionDomains", "@container": "@set"},
    "showFeatured": "toot:showFeatured",
    "showMedia": "toot:showMedia",
    "showRepliesInMedia": "toot:showRepliesInMedia",
    "QuoteRequest": "https://w3id.org/fep/044f#QuoteRequest",
    "quote": {"@id": "https://w3id.org/fep/044f#quote", "@type": "@id"},
    "quoteUri": "http://fedibird.com/ns#quoteUri",
    "_misskey_quote": "https://misskey-hub.net/ns#_misskey_quote",
    "quoteAuthorization": {"@id": "https://w3id.org/fep/044f#quoteAuthorization", "@type": "@id"},
    "interactionPolicy": {"@id": "gts:interactionPolicy", "@type": "@id"},
    "canFeature": {"@id": "https://w3id.org/fep/7aa9#canFeature", "@type": "@id"},
    "canQuote": {"@id": "gts:canQuote", "@type": "@id"},
    "automaticApproval": {"@id": "gts:automaticApproval", "@type": "@id"},
    "manualApproval": {"@id": "gts:manualApproval", "@type": "@id"},
    "QuoteAuthorization": "https://w3id.org/fep/044f#QuoteAuthorization",
    "interactingObject": {"@id": "gts:interactingObject", "@type": "@id"},
    "interactionTarget": {"@id": "gts:interactionTarget", "@type": "@id"},
    "FeatureRequest": "https://w3id.org/fep/7aa9#FeatureRequest",
    "FeaturedCollection": "https://w3id.org/fep/7aa9#FeaturedCollection",
    "FeaturedItem": "https://w3id.org/fep/7aa9#FeaturedItem",
    "FeatureAuthorization": "https://w3id.org/fep/7aa9#FeatureAuthorization",
    "topic": {"@id": "https://w3id.org/fep/7aa9#topic", "@type": "@id"},
    "featuredObject": {"@id": "https://w3id.org/fep/7aa9#featuredObject", "@type": "@id"},
    "featureAuthorization": {"@id": "https://w3id.org/fep/7aa9#featureAuthorization", "@type": "@id"},
}

# The namespaces a term an inline context defines may map into.
KNOWN_LD_NAMESPACES = (
    'https://www.w3.org/ns/activitystreams#', 'https://w3id.org/security#', 'http://purl.org/dc/terms/',
    'http://www.w3.org/2001/XMLSchema#', 'http://www.w3.org/ns/ldp#', 'http://www.w3.org/2006/vcard/ns#',
    'http://joinmastodon.org/ns#', 'http://schema.org#', 'http://schema.org/', 'http://ostatus.org#',
    'https://w3id.org/fep/', 'http://fedibird.com/ns#', 'https://misskey-hub.net/ns#',
    'https://gotosocial.org/ns#', 'https://join-lemmy.org/ns#', 'http://litepub.social/ns#',
    'https://joinpeertube.org/ns#',
)

_TERM_DEFINITION_KEYS = {'@id', '@type', '@container'}
_INLINE_CONTAINERS = {'@list', '@set', '@language'}
# pyld's own absolute-IRI test (jsonld._is_absolute_iri) without its blank-node alternative: a value that fails it
# is dropped from the graph by toRDF, and a blank-node label can be renamed without changing the normalized graph.
_ABSOLUTE_IRI = re.compile(r'^[A-Za-z][A-Za-z0-9+-.]*:[^\s]*$')
_LD_OPTIONS = {'documentLoader': ld_document_loader}


class LDCoverageError(ValueError):
    """The document's JSON says something its signed RDF graph does not."""


def _process_context(active_ctx, local_ctx):
    return jsonld.JsonLdProcessor().process_context(active_ctx, local_ctx, _LD_OPTIONS)


def _shape(definition: dict) -> tuple:
    """A pyld term definition reduced to what decides the triples a key produces."""
    return definition['@id'], definition.get('@type'), tuple(definition.get('@container', ()))


def _canonical_definitions() -> dict[str, set[tuple]]:
    """term -> the definitions it may have: the bundled contexts', plus pyfedi's and Mastodon's extension terms.
    Extension prefixes (toot, schema, ...) are left out: what matters is the IRI a term resolves to, not the
    prefix it was spelled with."""
    allowed: dict[str, set[tuple]] = {}
    bundled = _process_context(_process_context(None, None), list(BUNDLED_LD_CONTEXTS))
    for term, definition in bundled['mappings'].items():
        allowed.setdefault(term, set()).add(_shape(definition))
    for extension in (PYFEDI_CONTEXT_TERMS, MASTODON_CONTEXT_TERMS):
        extended = _process_context(bundled, extension)
        for term in extension:
            if not extended['mappings'][term]['_prefix']:
                allowed.setdefault(term, set()).add(_shape(extended['mappings'][term]))
    return allowed


CANONICAL_LD_TERMS = _canonical_definitions()
CANONICAL_LD_IRIS = {iri for shapes in CANONICAL_LD_TERMS.values() for iri, _type, _container in shapes}


def _check_context_entries(local_ctx) -> list[dict]:
    """The raw @context value: bundled URLs and inline term maps only. Returns the inline maps."""
    entries = local_ctx if isinstance(local_ctx, list) else [local_ctx]
    inline = []
    for entry in entries:
        if isinstance(entry, str):
            if entry not in BUNDLED_LD_CONTEXTS:
                raise LDCoverageError(f'JSON-LD context {entry} is not bundled, and is not fetched')
        elif isinstance(entry, dict):
            for term, definition in entry.items():
                if term.startswith('@') or ':' in term:
                    raise LDCoverageError(f'inline @context key {term!r} is not a plain term')
                if isinstance(definition, dict):
                    term_type = definition.get('@type', '@id')
                    container = definition.get('@container', '@set')
                    if not set(definition) <= _TERM_DEFINITION_KEYS or not isinstance(definition.get('@id'), str) \
                            or not isinstance(term_type, str) or term_type in ('@vocab', '@json', '@none') \
                            or not isinstance(container, str) or container not in _INLINE_CONTAINERS:
                        raise LDCoverageError(f'inline @context term {term!r} has an unsupported definition')
                elif not isinstance(definition, str):
                    raise LDCoverageError(f'inline @context term {term!r} has an unsupported definition')
            inline.append(entry)
        else:
            raise LDCoverageError('@context holds something other than a URL or a term map')
    return inline


def _check_inline_term(term: str, definition: dict) -> None:
    shape = _shape(definition)
    if term in CANONICAL_LD_TERMS:
        if shape not in CANONICAL_LD_TERMS[term]:
            raise LDCoverageError(f'inline @context redefines {term!r}')
        return
    iri = shape[0]
    if not iri.startswith(KNOWN_LD_NAMESPACES):
        raise LDCoverageError(f'inline @context maps {term!r} outside the known namespaces')
    if iri in CANONICAL_LD_IRIS:
        raise LDCoverageError(f'inline @context makes {term!r} an alias of {iri}')


def _is_compact_iri(item: str, active_ctx) -> bool:
    """'as:Public' rather than the absolute IRI: the graph cannot tell the spellings apart, pyfedi's string tests can."""
    prefix, colon, suffix = item.partition(':')
    return bool(colon) and not suffix.startswith('//') and active_ctx['mappings'].get(prefix) is not None


def _check_iri_values(key: str, value, active_ctx) -> None:
    for item in value if isinstance(value, list) else [value]:
        if isinstance(item, str) and not _ABSOLUTE_IRI.fullmatch(item):
            raise LDCoverageError(f'{key!r} value {item!r} is not an absolute IRI, so the signature does not cover it')
        if isinstance(item, str) and _is_compact_iri(item, active_ctx):
            raise LDCoverageError(f'{key!r} value {item!r} is a compact IRI; only the absolute spelling is accepted')


def _check_type_values(key: str, value, active_ctx) -> None:
    for item in value if isinstance(value, list) else [value]:
        definition = active_ctx['mappings'].get(item) if isinstance(item, str) else None
        if definition is not None:
            if definition['@id'].startswith('@'):
                raise LDCoverageError(f'{key!r} value {item!r} names a keyword')
        elif not isinstance(item, str) or not _ABSOLUTE_IRI.fullmatch(item):
            raise LDCoverageError(f'{key!r} value {item!r} is not a defined type or an absolute IRI')
        elif _is_compact_iri(item, active_ctx) or any(
                term is not None and term['@id'] == item for term in active_ctx['mappings'].values()):
            raise LDCoverageError(f'{key!r} value {item!r} spells a defined type another way; only the term is accepted')


def _check_node(node, active_ctx) -> None:
    if isinstance(node, list):
        for item in node:
            _check_node(item, active_ctx)
        return
    if not isinstance(node, dict):
        return
    if '@context' in node:
        inline = _check_context_entries(node['@context'])
        try:
            active_ctx = _process_context(active_ctx, node['@context'])
        except jsonld.JsonLdError as e:
            raise LDCoverageError(f'@context could not be processed: {e}') from e
        for term_map in inline:
            for term in term_map:
                _check_inline_term(term, active_ctx['mappings'][term])
    for key, value in node.items():
        if key == '@context':
            continue
        definition = active_ctx['mappings'].get(key)
        if definition is None:
            raise LDCoverageError(f'{key!r} is not defined by the document\'s contexts, so the signature does not cover it')
        iri = definition['@id']
        if iri == '@id':
            if not isinstance(value, str):
                raise LDCoverageError(f'{key!r} is not a string')
            _check_iri_values(key, value, active_ctx)
        elif iri == '@type':
            _check_type_values(key, value, active_ctx)
        elif '@language' in definition.get('@container', ()):
            continue        # a language map: its keys are language tags, and its strings are signed with them
        else:
            if definition.get('@type') == '@id':
                _check_iri_values(key, value, active_ctx)
            _check_node(value, active_ctx)


def check_ld_coverage(document: dict) -> None:
    """Refuses (LDCoverageError) a document whose JSON an LD signature does not actually cover.

    An RsaSignature2017 signs the URDNA2015 normalization of the document's RDF graph, but pyfedi reads the JSON
    keys. Anything that changes the JSON without changing the graph keeps the signature valid, so a signed
    document is accepted only when every key and value pyfedi could read is pinned to the graph:

    1. Every @context, at the top level and in any nested object (lists included), is a bundled context URL
       (BUNDLED_LD_CONTEXTS) or an inline term map. Inline maps exist because Mastodon sends one with every
       LD-signed activity; one is accepted only if each of its terms is a plain term (no '@' keywords, no
       compact-IRI terms) whose definition is a string, or an object of '@id', '@type' (not '@vocab'/'@json'/
       '@none') and '@container' ('@list', '@set' or '@language' only), and:
       - a term the bundled contexts, pyfedi or Mastodon already define (CANONICAL_LD_TERMS) keeps one of
         those definitions. Redefining 'content' as a blank node and adding 'realcontent' for as:content, or
         'id' as an alias elsewhere, is the remap that moved forged text into the key pyfedi reads;
       - any other term maps into a known namespace (KNOWN_LD_NAMESPACES), never to a keyword ('realid':
         '@id'), a blank node, or an IRI a canonical term already uses (an alias that would let a signed
         value be moved out of, or into, a key pyfedi reads).
    2. Every key, recursively except inside language maps, is a term the active context defines. With the
       activitystreams context's '@vocab': '_:' an undefined key becomes a blank-node property, which
       normalization drops, so its value was never signed ('sensitive', 'stickied' without a context).
       Other '@' keywords and absolute-IRI keys are refused for the same reason.
    3. Every 'id', and every string under an '@id'-typed term ('actor', 'inReplyTo', 'to', ...), is an
       absolute IRI, and every 'type' a defined term or absolute IRI: a relative IRI is dropped from the
       graph, and a blank-node label can be swapped for another without changing the normalized hash.
       Compact IRIs ('as:Public', 'as:Note') are refused there, and so is a 'type' spelled as the absolute
       IRI of a defined term: the graph cannot tell the spellings apart, pyfedi's string comparisons can.

    This is defence in depth, not a proof that the JSON equals the graph (ruling R-LDDOWNGRADE). Accepted
    residuals: node merging (properties moved between JSON occurrences of one @id), ordering, multiplicity
    and shape (list order, duplicates, scalar versus list, a string versus {"id": ...}), and renames between
    non-canonical terms that map to one IRI. They can still change what pyfedi reads from the activity
    itself (its type-specific fields, e.g. a Like's or Follow's), but not post content or addressing: an
    LD-accepted Create or Update has its object, and its to/cc/audience/target, replaced by the origin's
    copy; an LD-accepted Delete needs the origin to have stopped serving the object
    (app.activitypub.util.verify_ld_accepted_activity); an LD-accepted Announce's inner Create, Update or
    Delete gets the same treatment (verify_announced_inner, ld_signed); rows logged for such a delivery are
    never replayed (LD_SIGNED_LOG_PREFIX); and null values and empty lists are stripped after verification
    (strip_empty_values).
    """
    try:
        _check_node(document, _process_context(None, None))
    except RecursionError:
        # The gate recurses once per level of nesting, and JSON can nest past the interpreter's recursion limit
        raise LDCoverageError('the document is nested too deeply to check') from None


def _is_empty(value) -> bool:
    return value is None or value == [] or value == {}


def strip_empty_values(node) -> None:
    """In place, bottom-up: drops null values, empty lists and empty objects.

    Run on an LD-verified document so that pyfedi never reads a value the signature did not cover: a null
    produces no triple, so an injected 'commentsEnabled': null or 'sensitive': null keeps the normalized hash
    but would otherwise lock a post or clear a flag. It only ever removes: an empty list or object can itself
    be signed (rdf:nil for an empty @list, a blank node for {}), and stripping it loses that signed value,
    which pyfedi then reads as absent. A peer's 'summary': null or 'inReplyTo': null simply becomes absent."""
    if isinstance(node, dict):
        for key in list(node):
            strip_empty_values(node[key])
            if _is_empty(node[key]):
                del node[key]
    elif isinstance(node, list):
        for item in node:
            strip_empty_values(item)
        node[:] = [item for item in node if not _is_empty(item)]
