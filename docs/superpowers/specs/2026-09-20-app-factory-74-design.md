# Sub-project 74: `app/__init__.py` — the factory's configuration branches

**Status:** design approved, ready to implement
**Branch:** `blentz`, at `d5b160646`
**Predecessor:** sub-project 73, which closed both remaining form modules — 52 floors.

## Goal

Cover the app factory's config-conditional branches and `get_locale`'s four
arms, and take a floor. The module stands at **81.938%** with 26 missing lines.

This round exists partly because sub-project 73 repaired `get_locale` and left
its siblings uncovered: the `or 'en'` that D864 added is exercised only
indirectly, through a reminder test two modules away.

## Targets

| region | where | why it is missed |
|---|---|---|
| `get_locale` | `:31`, `:33`, `:44-45` | the logged-in, session and exception arms |
| `SERVER_URL` in mixed mode | `:140` | `HTTP_PROTOCOL == 'mixed'` |
| Sentry | `:145-146` | `SENTRY_DSN` unset in every existing config |
| API docs | `:157-163` | `SERVE_API_DOCS` unset |
| three OAuth registrations | `:234`, `:244`, `:255` | Google, Mastodon and Discord client ids unset |
| the error mailer | `:333-346` | `MAIL_SERVER` and `ERRORS_TO` unset |
| `os.mkdir('logs')` | `:350` | the directory already exists in the container |

Everything is reachable by building a second app from a config subclass:
measured at **0.33s** per build, with every branch above firing.

## THE SHARED LOGGER, WHICH COMES FIRST

`Flask.logger` is `logging.getLogger(app.name)`, and every app built from this
package is called `app` — so **the session's app and any app a test builds share
one logger object**. Handlers accumulate on it globally:

```
PROBE e1 handlers: ['RotatingFileHandler', 'SMTPHandler', 'RotatingFileHandler']
```

That is not cosmetic. The factory attaches an `SMTPHandler` at `ERROR` level
when `MAIL_SERVER` and `ERRORS_TO` are set, and `MAIL_SUPPRESS_SEND` does not
apply to a logging handler — it is a `smtplib` client, not Flask-Mail. A test
that builds such an app and does not clean up leaves every later test in the
session one `app.logger.error(...)` away from a real SMTP connection attempt to
`smtp.example`.

Every row here that builds an app therefore restores the logger's handler list,
through a fixture rather than by hand, so a failing assertion cannot skip it.

## No production change

Nothing in this round is a repair. The factory's branches are configuration the
test environment has never set, not defects — which is worth stating, because
every round since 66 has found at least one, and "no defect" is a result rather
than an omission. Three shapes were examined and are **registered** below.

## Registered, not fixed

| # | Where | What | Why not this round |
|---|---|---|---|
| R1 | `:44`, `:43` | **`get_locale` has two bare `except:` clauses**, which catch `KeyboardInterrupt` and `SystemExit` along with everything else. The outer one also wraps the `current_user` access, so a genuine database failure while reading `interface_language` is silently answered `'en'`. | Pre-existing and shared with much of `app/`; changing what a failure does here is not a coverage round's call. Both arms are covered by this round either way. |
| R2 | `:349-350` | **`if not os.path.exists('logs'): os.mkdir('logs')`** runs at import-time-ish in the factory and is relative to the PROCESS's working directory, so where the log lands depends on where the app was started from. | A deployment concern, not a correctness one. |
| R3 | `:333-346` | **The error mailer is built from eight config values with no validation**: a `MAIL_SERVER` set without `MAIL_PORT` yields `mailhost=(server, None)`, which `SMTPHandler` accepts and fails on only when an error is first logged. | The failure is at a distance but the configuration is an operator's, and the round would be guessing at what validation is wanted. |

## Success criteria

- `app/__init__.py` at `[]`/`[]` on the **full-suite** run, or every residual
  arc declared unreachable with a named cause and a proof.
- A floor of 100 — 53 floors.
- `git diff --numstat` names **no file under `app/`**: this round is tests only.
- The shared-logger cleanup is a fixture, and a row asserts it works.
- Suite green; floors checked with `&&`; a mutation pass over `get_locale`,
  anchors checked first — D833.
- Findings registered from **D869**; `tests/README.md` facts from **338**.
