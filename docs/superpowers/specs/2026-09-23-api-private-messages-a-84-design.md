# Sub-project 84 slice A: `app/api/alpha/utils/private_message.py`

**Status:** slice A complete. THREE production defects fixed (D1167–D1169),
two dead arms removed (D1170, D1171), one equivalent mutant recorded (D1172).
**Branch:** `blentz`
**Predecessor:** sub-project 83, which closed the authentication surface.
**72 floors, rising to 73.**

## Why this module next

The standing instruction puts hosted-site security first. With authentication
closed, the lowest-covered security-bearing code is the API:

| module | statements | covered |
|---|---|---|
| `app/api/alpha/utils/misc.py` | 719 lines | 3.1% |
| `app/api/alpha/utils/user.py` | 1031 lines | 4.5% |
| `app/api/alpha/utils/feed.py` | 220 lines | 7.3% |
| `app/api/alpha/utils/private_message.py` | 414 lines | 7.3% |
| `app/api/alpha/utils/reply.py` | 793 lines | 8.1% |

Private messages first: they are the one surface on the instance whose whole
purpose is that nobody else reads them.

`app/api/alpha/utils/private_message.py` was **7.3%** and is now **100%**.

## THE PRODUCTION CHANGES

### Any account could put any conversation in front of the admins (D1167)

```python
if not (conversation or conversation.is_member(user) or user_access("administer all users", user.id)):
    raise Exception("You are not a part of this conversation")
```

`or`, where each arm was meant to be required. A conversation that **exists**
makes the whole disjunction true, so `not` is false and nothing is refused.
Measured:

```
PROBE ba1 outcome: accepted | reports filed: 1
PROBE ba2 1 reports, bodies: ['SECRETBODY']
```

The second measurement is what makes it serious.
`get_private_message_conversation_report_list` hands an administrator the
**message history** of every reported conversation — so any account could
walk conversation ids and have other people's private messages read by the
instance's staff, one report at a time.

The same expression's other half: with no such conversation,
`conversation.is_member` was an `AttributeError` on None.

### The documented way to read a conversation was a KeyError (D1168)

`person_id = int(data['person_id'])` stood at the top of
`get_private_message_conversation`, **ten lines above** the `if 'person_id' in
data` written to guard it. So every call that did not pass one — including the
`conversation_id` form the endpoint documents — was:

```
PROBE ba3 outcome: KeyError: 'person_id'   (conversation_id given)
PROBE ba6 outcome: KeyError: 'person_id'   (neither given)
```

Behind it sat a second bug the first one hid: `joined_conversations` came from
`.scalars()`, a **one-shot iterator**. The membership test consumed it and the
query below filtered on it again, so once the KeyError was fixed a legitimate
member still got an empty conversation. `.all()` materialises it.

### `db.session.get` answers None (D1169)

Both resolve endpoints read `.targets` / `.suspect_conversation_id` straight
off it:

```
PROBE ba7 outcome: AttributeError: 'NoneType' object has no attribute 'targets'
```

### Two dead arms (D1170, D1171)

* `post_leave_conversation` tested `if conversation.is_member(user):`
  immediately below the guard that has already refused everyone it would have
  excluded;
* `post_private_message_report` built `already_notified = set()` and tested
  `if admin.id not in already_notified:` — **nothing ever added to the set**.
  A de-duplication that de-duplicates nothing reads like the question has been
  dealt with. It has not been; `Site.admins()` answers distinct rows, so there
  is nothing here to de-duplicate, and D1162 was the same question answered
  wrongly one module away.

## D1172, an equivalent mutant

`if conversation_ids and joined_conversations:` — dropping the second conjunct
changes nothing, because the query it guards filters
`ChatMessage.conversation_id.in_(joined_conversations)` and an empty list
yields the same empty answer.

## The mutation pass

30 mutants, 28 killed on the measuring pass. One survivor was **my own
mutant**, not a gap: `filter(..., True)` adds nothing to a SQLAlchemy filter,
so it could not fail. Rewritten to drop the `recipient_id != None` clause
outright, it needed a row for a message that has not been addressed yet —
which is a real state, because `send_message` writes the row before it knows
who the other member is. The other survivor is D1172.

## Success criteria

- `app/api/alpha/utils/private_message.py` at **100%**, 0 functions with gaps.
- D1167–D1169 land with their pins inverted.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1167**; `tests/README.md` facts from **548**.
