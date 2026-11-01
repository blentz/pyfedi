# One multi-image post: v1.8.10's gallery type, carrying the Pixelfed album

Owner ruling, 2026-11-01: v1.8.10's gallery post type wins over the branch's album (an image post whose other
pictures were its `post.gallery`); the album's interop and features move onto it, and the branch's local album
and v1.8.10's gallery upload become one feature.

## Decisions

**G-1 Type.** A post with two or more pictures is `POST_TYPE_GALLERY`; one picture is `POST_TYPE_IMAGE` and has
no gallery rows. This holds for an inbound Create or Update, the web forms and the API. A link post with two or
more images beside its link (Friendica) stays a link post with a gallery, as before.

**G-2 Storage.** `post.gallery` holds every picture of a gallery post, in order, `post_file.weight` 0, 1, 2, ...;
`post.gallery_count` is their number. A row of weight `GALLERY_BLOCKED_WEIGHT` still remembers a blocked image an
Update brought. `post.image` is the thumbnail: for a local gallery the composite v1.8.10 builds; for an inbound
one the peer's `image`, else a copy of the first picture (no composite: that would fetch every picture on the
inbox path, D775). The thumbnail is never also a gallery row.

**G-3 Url.** A local gallery's `post.url` is its own page, as v1.8.10 sets it for Lemmy clients. An inbound
gallery's is its first picture, so an Update that sends the same album changes nothing (v1.8.10 stores none,
and every Update would then rebuild the image).

**G-4 Out.** A gallery federates every picture, in order, as the album did (Pixelfed interop R5): Documents, a
WebP, AVIF or HEIC as its JPEG rendition (R3). The Page form (Lemmy, PieFed) adds v1.8.10's Link to the post's
page; the Note form (Mastodon, Pixelfed) does not.

**G-5 API.** A gallery post's view carries v1.8.10's `gallery` (urls) and the branch's `extensions.gallery`
(url, alt text, size), now every picture. On create and edit, `gallery` is every picture of a gallery post
([{url, alt_text}], two to `max_album_images()`); given, the post is a gallery and `url` is not used. On edit,
leaving it out keeps the pictures; a list of fewer than two is refused.

**G-6 Web.** The image form takes one picture. A gallery is made and edited with v1.8.10's gallery form, at most
`max_album_images()` pictures (the admin setting, default 4); the branch's album slots on the image form and the
edit page's album controls are removed.

**G-7 Display.** The post page shows a gallery's pictures as the album was shown (grid, lightbox, blur and
content warning as for an image post); the teaser badge counts `gallery_count`; the Galleries filters (G1 of the
galleries rulings: image posts) take gallery posts too; RSS lists each picture as media:content.

**G-8 Migration.** Every image post with an album (`gallery_count > 0`) becomes a gallery post: a new File copying
its image's url, alt text, size and hash (not its file on disk) is inserted at weight 0 ahead of the album's
weights 1.., `gallery_count` counts it, and `post.url` keeps the first picture. The image File stays as the
thumbnail. Reversible: the downgrade removes the weight-0 rows and restores the type.
