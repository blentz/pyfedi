# Galleries filters and icon filter navs

Date: 2026-10-30
Status: owner design and rulings G1-G4 given in conversation; building

## 1. Problem

Pixelfed seeding works (spec 2026-10-29): on hell.cloud the instance actor follows 50 accounts and their posts land in
`microblogs`. But `microblogs` takes about 38,000 relayed posts a day and is left out of All and Popular, so a seeded
Pixelfed post ranked around 33,800th by New. The owner's design surfaces image posts as a *filter*, not a community:

- the home page's Media filter includes galleries;
- `microblogs` filters by Posts, Comments, Events and Galleries;
- the People pages get a Galleries filter;
- the filter button navs become icons with hover text, alt text and no-JS alternatives, since whole words no longer
  fit.

### Owner rulings (2026-10-30)

- G1. A Galleries post is any image post (`Post.type == POST_TYPE_IMAGE`), from any platform. Shape, not software:
  interop ruling D12 stands. Mastodon photo posts are included, accepted.
- G2. The home Media filter shows gallery posts beside PeerTube and Castopod content, although the home feed keeps
  titleless microblog posts out (`MICROBLOG_GATE`). The gate is lifted for gallery posts in the Media filter only.
- G3. The People pages (`/instance/<server>/people`) get a Galleries filter listing the Pixelfed accounts known here.
- G4. The home view filter and the community content and sort navs (shared with topics) become one icon button nav:
  an icon per choice, its name as hover text (`title`) and as text for screen readers, the current choice marked
  `aria-current`, and every choice a plain link, so nothing needs JavaScript.

## 2. Design

### 2.1 Galleries in microblogs (G1)

`?content_type=galleries` on a community: the posts query (`community_post_query`) adds `Post.type ==
POST_TYPE_IMAGE`. The Galleries button shows on `microblogs` (and its per-server views), as Events shows only where a
community has events. An unknown `content_type` keeps falling back as today. The Live view stays posts-only.

### 2.2 Galleries in the home Media filter (G2)

`get_deduped_post_ids` gains a gallery source used only by the Media filter: image posts in a community shown in All
or in `microblogs`, listable (`listable_sql`), without `MICROBLOG_GATE`. The existing Media source (PeerTube and
Castopod communities) is unchanged. The Media Live fragment uses the same sources.

### 2.3 Galleries on the People pages (G3)

`/instance/<server>/people?filter=galleries` lists accounts whose instance's software is `pixelfed`, with the page's
search still applying. A Galleries link sits beside the search box.

### 2.4 One icon filter nav (G4)

A macro, `filter_nav(choices, current)`, renders a `btn-group` of links, each an icon (`fe fe-*`), `title` and
`aria-label` set to the choice's name, a visually hidden name for screen readers, `aria-current="page"` and
`btn-primary` on the current one. With icons the group fits a phone, so the home page's mobile `<select>` (which does
nothing without JavaScript) is replaced by the same nav, and the community nav's mobile content-type select keeps
working as a GET form. Icons: existing Feather glyphs (`fe-image` for Galleries; the others keep or take glyphs
already in the font). Applies to `_view_filter_nav.html` (and the dillo theme's copy) and the community content and
sort groups in `community/_community_nav.html`.

## 3. Tests

Unit: the galleries content filter (image posts only; button only on microblogs); the Media filter including image
posts that the gate would hide, and nothing else ungated; the People Galleries filter; the nav macro (icons, title,
aria-label, aria-current, plain links, no select). Golden documents and tests that pin the old button text are updated.
100% line and branch coverage on new and changed lines.

## 4. Build order

1. The filter nav macro, applied to the home view filter and the community navs.
2. Galleries in microblogs.
3. Galleries in the home Media filter.
4. Galleries on the People pages.
