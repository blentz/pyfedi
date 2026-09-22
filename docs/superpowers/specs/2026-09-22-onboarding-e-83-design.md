# Sub-project 83 slice E: `app/auth/onboarding.py`

**Status:** slice E complete. SIX production defects fixed (D1152–D1156,
D1158), one dead function removed (D1157).
**Branch:** `blentz`
**Predecessor:** slice D, which closed `app/ldap_utils.py` and fixed
D1145–D1151. **69 floors, rising to 70.**

## Scope

The three screens a new account sees: the instance chooser, the content
filters, and the topics — `onboarding_instance_chooser`, `filter_selection`,
`choose_topics`, `join_topic`, `topics_for_form` and `send_community_follow`.

`app/auth/onboarding.py` was **15.1%** and is now **100%**.

## THE PRODUCTION CHANGES

### The filter screen created the filter only for people who had it (D1152)

```python
existing_filters = Filter.query.filter(..., Filter.title == 'Trump & Musk').first()
if existing_filters is not None:
    content_filter = Filter(title='Trump & Musk', ...)
    db.session.add(content_filter)
```

Inverted. Measured:

```
PROBE ay1 status: 302 | filters now: []
PROBE ay2 filters now: 2 | titles: ['Trump & Musk', 'Trump & Musk']
```

So the screen never did the one thing it exists for, and an account that came
back to it collected a duplicate each time. The other four answers on the same
form were written correctly, which is why the screen looked like it worked.

### Onboarding joined private communities (D1154)

```
PROBE ay4 status: 302 | member of the private community: True
```

`Community.private` is invite-only real access control (app/models.py:594):
every other surface requires a join request and somebody's approval, and six
defects in this campaign have been private-community content escaping. This
route wrote the `CommunityMember` row directly, for every private community
that happened to carry a chosen topic. Fact 478 at a seventh surface.

### A topic id that was not a number (D1153)

`int(topic_id_str)` over `request.form.getlist('chosen_topics')`, which is
whatever was posted:

```
PROBE ay3 outcome: ValueError: invalid literal for int() with base 10: 'nonsense'
```

### Looking at the page finished onboarding (D1155)

`mark_onboarding_as_finished()` was the route's first line, so a GET was
enough:

```
PROBE ay5 finished_onboarding after a GET: True
```

Somebody who opened the topics page and went elsewhere was never brought back
to it. It is finished when they answer — and on the arm where the instance has
no topics to ask about, or every login would return to a question this
instance cannot ask.

### "You have joined some communities" when nothing was joined (D1156)

```
PROBE ay6 said: You have joined some communities relating to those interests.
```

Said for a topic with no communities in it, and now for one whose communities
this account may not join — which D1154's fix makes a real case. `join_topic`
answers how many memberships it wrote, and the message follows the count.

### D1157, a second copy of a live function

`create_user_application(user, registration_answer)` stood in
`app/auth/util.py`, called from nowhere. It **ignored its own
`registration_answer`** and wrote `answer='Signed in with Google'` for every
caller, and it set no `status`, where `create_registration_application` — the
live function four lines below it — sets -1 when the address is unverified so
an application cannot be approved before the email is. A second, wronger copy
of a live function is how a fix reaches one of them and not the other.

### D1158, an arm that could not be taken

`topics_for_form`'s root loop tested `if node is not None`, which cannot
happen: `build_topic_tree` answers None only past depth 2 and these are the
roots, at depth 0. The test inside the recursion, where the cap bites, stays.

## The mutation pass

22 mutants, 19 killed on the measuring pass. All three survivors were **rows
of mine**, not defects:

* the depth cap — the row read the rendered page, and the template renders
  three levels regardless, so it could not tell the cap from the template;
* the country pre-selection — the row asserted the topic appeared on the page
  rather than that it was selected;
* the membership test — its observable effect is not a duplicate row (the
  second check catches that) but a **second Follow and join request sent to a
  remote community** this account already belongs to.

All three now assert the thing the guard actually decides. 22/22.

## Success criteria

- `app/auth/onboarding.py` at **100%**, 0 functions with gaps.
- D1152–D1156 land with their pins inverted.
- Suite green; floors checked with `&&`; a mutation pass, anchors first (D833).
- Findings from **D1152**; `tests/README.md` facts from **541**.
