# Fuzz every external input surface — design

Date: 2026-10-13. Owner rulings (2026-10-13): fuzz all surfaces, tiered; fix
every finding with TDD, one commit per defect; three gates — registry
completeness test, smoke campaign script, ReDoS time budget in the corpus replay.

## Problem

`tests/fuzz/run_campaign.py` has three libFuzzer targets (`allowlist_html`,
`is_valid_xml_utf8`, `sanitize_svg_bytes`). `tests/test_link_parsers_fuzz.py`
adds a deterministic FuzzedDataProvider loop over three link parsers. Every
other parser of remote or user-supplied bytes — HTTP/RFC 9421/LD signatures,
the capped body reader, AP JSON extractors, markdown rendering, webfinger and
discovery, RSS credits, NNTP, API schemas, image decode — is unfuzzed, and an
inventory read found ~10 likely crashes and ~11 regexes with backtracking shape.

## Structure

`tests/fuzz/harnesses.py` becomes a package, `tests/fuzz/targets/`, one module
per surface. Each module exports property checks (plain functions taking
`bytes`, raising `AssertionError` on a property break, no atheris import) and a
`TARGETS` list of `Target` records:

```python
@dataclass(frozen=True)
class Target:
    name: str              # corpus dir name, campaign CLI name
    check: Callable[[bytes], None]
    surface: str           # key into SURFACES
    budget_ms: int | None  # per-input CPU-time budget in replay; None = untimed
    needs_app: bool        # check must run inside a Flask app context
```

`tests/fuzz/targets/__init__.py` collects `REGISTRY: dict[str, Target]`
(duplicate names are an import-time error). `tests/fuzz/harnesses.py` stays as
a re-export shim for the three existing checks so nothing else moves.

`run_campaign.py` imports the registry inside `atheris.instrument_imports`
and keeps its corpus/work/artifact layout. Targets with `needs_app` get one app
context pushed before `atheris.Fuzz()` (built from the test config, no DB
access — a check that touches the DB is a harness bug). Targets with a budget
pass `-timeout=<ceil(budget*10 s)>` by default so libFuzzer reports a slow unit
as a timeout artifact.

Checks parse inputs with `atheris.FuzzedDataProvider` only where structure is
needed; that is allowed in the check module only via a tiny local decoder that
does not import atheris (the replay must run without it) — in practice checks
decode bytes themselves (utf-8 with `errors='replace'`, `json.loads` with
fallback to a string value, a length-prefixed split for multi-argument targets).

## Surfaces and targets

`tests/fuzz/surfaces.py` holds `SURFACES: dict[str, list[str]]` — surface key
to the dotted names of the production functions it covers. This is the
committed manifest of external input entry points. The tiers below are the
execution order.

**T1 signatures and transport** — `signature.parse_signature_header`,
`HttpSignature.parse_signature`, `signature.parse_http_date`/`parse_ld_date`,
`signature_rfc9421` dictionary parser, `rfc9421.parse_key_id`,
`rfc9421.check_content_digest`, `rfc9421.verify` (fixed key; never accepts a
foreign keyid), `LDSignature.verify_signature` + `normalized_hash` (fixed key,
bundled loader only), `ld_contexts.check_ld_coverage`/`strip_empty_values`,
`utils._read_capped_body` (fake stream: size cap, decompression bombs),
`utils.accepted_media_ranges`.
Property: only the documented exception types escape; parsers round-trip what
they claim to; nothing fetches the network.

**T2 AP JSON extractors** — `normalise_activity_types` (any JSON, including
non-dict), host/actor helpers (`host_of`, `authority_of`,
`extract_domain_and_actor`, `ensure_domains_match`, `normalise_actor_string`),
content helpers (`content_warning_from`, `language_variants_from`,
`media_url_from`, `gallery_attachments`, `posting_warning_from_ap`,
`quote_id_from`, `activitypub_visibility`, `parse_ban_expiry`,
`castopod_episode_url`), `entitlement._owner_candidates`/`addressees_of`, models
coercers (`_as_*`, `image_url_from`, `more_info_url_from`,
`actor_name_from_ap`, `parse_ap_timestamp`/`parse_ap_published`,
`property_value_fields`).
Property: output types and length caps hold; every URL out is http(s), passes
`url_is_storable`, and (where documented) is on the owner's host; no exception.

**T3 markdown, HTML, ReDoS** — `markdown_to_html` (`test_env`), each
preprocessor, `community/feed/person_link_to_href` (`server_name_override`),
`html_to_text`, `markdown_to_text`, `shorten_string`, `microblog_content_to_title`,
`microblog_content_to_link`, `strip_quote_fallback`, `EnhancedImages`; timed
regex targets for `LINK_PATTERN`, `PERSON/COMMUNITY/FEED_PATTERN`,
`MENTION_PATTERN`, `_md_img_with_attrs_re`, allowlist spoiler/strike regexes,
`re_bold`/`re_em_bold`, the blockquote regex, `_CASTOPOD_EPISODE_LINK`,
discovery `_HOSTNAME`, `EMAIL_RE`.
Property: markdown output satisfies the existing allowlist property; no `<a>`
nested in `<a>`/`<code>`; length caps; regex targets finish inside budget on
inputs up to 64 KiB.

**T4 URLs, redirects, discovery, feeds** — `url_is_storable`, `url_scheme`,
`has_unsafe_url_scheme`, `url_host`, `inbox_domain`, `is_safe_redirect_target`,
`is_invalid_get_request_uri` (patched `getaddrinfo`), discovery `filters`
(`request_host`, `clean_https_url`, `clean_name`), discovery entry parsers
(mastodon/pixelfed/peertube/castopod), `external_search.video_from`,
`credits.parse_feed`/`parse_feed_credits`/`_normal_url`, `cli.strip_tracking`,
`requestor_domain`, webfinger resource parse in `process_webfinger_request`
(request context, domain-check path only).
Property: no exception; redirects never leave the origin or use a non-http(s)
scheme; discovery URLs are https on the stated host; DTD feeds give nothing.

**T5 NNTP** — `nntpserver.parse_datetime`, `parse_range` (bounded),
`server._parse_message_id`, `_sanitize_group_name`, and the connection handler
driven over a fake socket against a stub server (command dispatch, AUTHINFO,
GROUP/LISTGROUP/HDR/OVER/NEWNEWS, line decode).
Property: a malformed command gets an NNTP error reply, never kills the
handler; range work is bounded.

**T6 API and imports** — every marshmallow request schema in
`app/api/alpha/schema.py` `.load(...)` (one target, schema chosen by first
byte), `validate_*` helpers, `safe_order_by`, `expand_hex_color`; settings
import JSON (`import_entries` and the shape check in front of
`import_settings_task`); CSV follow-import decode/reader block (extracted to a
function if inline).
Property: only `ValidationError` from `.load`; import rejects non-dict JSON
cleanly.

**T7 image decode** — the `process_upload` decode sequence (Image.open,
exif_transpose, `to_srgb`, thumbnail) on BytesIO, `guess_mime_type`,
`mimetype_from_url`, `inspect_image_c2pa`.
Property: only `PIL.UnidentifiedImageError`/`OSError`/`ValueError`/
`DecompressionBombError` escape; output is RGB(A) within size caps.

Out of scope: functions that need the DB or the network for their parsing
(e.g. `find_community`, `canonical_handle`, `object_language`); their pure
parts are covered through the functions above. If a defect fix extracts a pure
helper from an inline view, that helper joins its surface.

## Gates

1. **Registry completeness** (`tests/test_fuzz_registry.py`): every `SURFACES`
   entry resolves to an importable callable; every surface has ≥1 target; every
   target's surface exists; every target has a non-empty
   `tests/fuzz/corpus/<name>/`; every corpus directory belongs to a target (no
   orphans); `run_campaign.TARGETS` is the registry.
2. **Replay with budget** (`tests/test_utils_fuzz_corpus.py`, generalised over
   the registry): each corpus file runs through its check; a target with
   `budget_ms` fails if one input exceeds it (measured with
   `time.thread_time`, the thread's CPU time, so time spent waiting for a CPU
   under load does not count; budgets are set ~10x the measured worst seed so
   CI noise does not flake). ReDoS seeds are committed at a size that takes seconds
   on the vulnerable regex, so a regression fails loudly and fast.
3. **Smoke campaign** (`tests/fuzz/smoke.py`, run as
   `python -m tests.fuzz.smoke [--seconds N] [targets...]` in the test
   container): runs every target in its own subprocess for N seconds (default
   20), outside `--cov`, and exits non-zero listing each target that produced a
   crash/timeout artifact. Its argument handling and result parsing have their
   own unit test (`tests/test_fuzz_smoke.py`) that does not start libFuzzer.

## Findings

Each finding: commit the minimised reproducer under its target's corpus with a
descriptive name, write a failing regression test next to the module's existing
tests, fix at the narrowest layer, one commit per defect. A fix that changes
observable behaviour beyond "refuse cleanly" is asked of the owner first.
`KNOWN_UNFIXED` stays the mechanism only for a finding the owner defers.
`tests/README.md`'s "Findings are reported, not fixed" paragraph is rewritten
to match.

## Done when

All seven tiers registered; all three gates committed and green; a 60 s smoke
run of every target is clean; full suite green; README Fuzzing section updated.
