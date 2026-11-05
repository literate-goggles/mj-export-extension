# Midjourney web API observations

Observed through the logged-in site's own Organize, Personalize, and Moodboards pages on 2026-10-27. These are private web endpoints, not a supported public API. This file contains paths and field names only. Authentication material and raw personal responses are deliberately excluded.

## Verified response schemas

All paths below are relative to `https://www.midjourney.com`. The exporter uses only GET and sends `x-csrf-protection: 1` from the page origin. It lets the browser attach the existing session. The header value came from the supplied crawler notes; successful exporter requests are the verification, rather than reading request authentication headers.

| Purpose | Method / path | Observed query keys | Response |
|---|---|---|---|
| Own Create / archive feed | GET `/api/imagine` | `user_id`, `page_size`; following page adds `cursor` | Object: `data`, `cursor`, `checkpoint` |
| Site's live feed refresh | GET `/api/imagine-update` | `user_id`, `page_size`, `checkpoint` | Same envelope; observed empty `data` and null cursor. The extension does **not** use this polling endpoint. |
| Own account name | GET `/api/profile` | `user_id` | `user_id`, `username_v2`, plus display/profile fields. Exporter retains only these first two fields. |
| Own moodboards, including images | GET `/api/moodboards` | None | Array of boards |
| Own personalization profiles | GET `/api/personalized-profiles` | None | `profiles` array and `counts` object |

### Pagination

The site's own archive requested `/api/imagine` again with `cursor`; a final observed response had empty `data`, `cursor: null`, `checkpoint: null`. The exporter follows opaque cursors until empty or the user-selected maximum. It rejects repeated cursors. `page_size` is capped at 100 in the exporter; the small test asks for 20. `checkpoint` is preserved in progress and forwarded with subsequent requests; the archive's observed next-page query itself used cursor without checkpoint. Ordering is not assumed for date filtering: since-date selection traverses the feed and filters timestamps.

### Job fields observed

`id`, `enqueue_time`, `full_command`, `width`, `height`, `batch_size`, `job_type`, `event_type`, `parent_id`, `parent_grid`, `personalization_codes`, `image_likes`, `image_hides`, `rating`, `published`, `shown`, `user_hidden`, `template`, `video_segments`.

A personalization reference has `code`, `id`, `type`, `user_id`. The exporter projects only necessary metadata; it does not save the full API response. Parent index mapping is accepted only when `parent_index` or `parent_grid` is an integer; unknown structures are retained as `parent_grid` and the normalized index stays null. Job kinds are classified only from explicit type labels, or a parentless multi-image batch as a grid. Unknown child kinds remain `other`.

### Moodboard fields observed

`moodboard_id`, `title`, `created`, `user_id`, `personalize`, `images`. Each image has `url`, `width`, `height`, `created`. No board version was present in the inspected schema. A human-usable code may be recovered from a matching job's `personalization_codes`; it is not fabricated from the numeric board ID.

### Profile fields observed

`profiles[]`: `id`, `created`, `majorVersion`, `rankingCount`, `service`, `title`, `type`, `user_id`.

`counts`: `main`, `mainV7`, `niji`, `nijiV7`, `mainBtrees`, `mainBtreesV7`, `nijiBtrees`, `profileCounts` (keyed by profile ID).

The UI showed global V6/V7 and Niji V6/V7 profile links, plus a custom V8 profile. Global profile IDs are represented by the UI as `<own-user-id>_<major-version>_<service>`. Only visible aggregate counts are exported; no individual preference/rating pairs are fetched.

## Images

An own job detail rendered an original image at `https://cdn.midjourney.com/<job-id>/0_0.jpeg` with natural dimensions 1024 × 1024. The archive separately rendered transformed thumbnail URLs ending in `_384_N.webp` with size/quality query parameters. The exporter does not use those thumbnails for job images. Page-context image fetches failed in this account/browser, so image files use `chrome.downloads` directly; the own-record allowlist is still checked in the page context.

It tries `0_<index>.jpeg`, with a `.png` fallback when Chrome reports unavailable/bad content (it does not expose an exact HTTP status for downloads); these alternatives must belong to a job in an observed own-feed response. Other errors follow bounded retry rules. Moodboard URLs are used exactly as returned: own-board images on `https://cdn.midjourney.com` (newer boards observed as `/u/<uuid>/<64 hex>.jpg`) and, on boards made before about May 2026, Midjourney short links `https://s.mj.run/<code>`. An 11-character code was observed in one export; the extension accepts 1 to 64 letters, digits, `-` and `_`. Chrome follows a short link to wherever it redirects. Where that is has not been observed, because no short link was requested while this was written. Images on any other origin are reported and not fetched. All successful bytes are downloaded unchanged. The local suffix is the URL's when that is `.png`, `.jpg`, `.jpeg` or `.webp`, else `.jpeg` (a short link has none), unless Chrome replaces it with the suffix of the type the server sent, as it does for PNG, WebP and GIF (Chromium source, and a local Chrome 148 test: [TESTING.md](TESTING.md)). The metadata records the name Chrome used.

## Chooser thumbnails

The "Choose what to export" window shows small pictures so designers can recognize their work. These are ordinary image loads by the Midjourney page, like the site's own: the chooser sets the source of an image element in its window. They are not API calls, they do not go through the read scheduler, the page bridge or `chrome.downloads`, and nothing is saved.

- A generation tries `https://cdn.midjourney.com/<job-id>/0_0_384_N.webp`, then `0_0.jpeg`, then `0_0.png`, and shows a neutral placeholder when all three fail. The `_384_N.webp` name is a best guess from the archive's own thumbnails, which were observed with size and quality parameters; whether the name works without them has not been verified, which is why the originals follow it. Only ids matching `^[a-f0-9-]{36}$` (any case) are used.
- A moodboard reference image uses its URL as returned, and only when its origin is `https://cdn.midjourney.com`; anything else, Midjourney short links included, shows a placeholder and is never loaded.
- Video jobs show a placeholder.
- Pictures load lazily, only for the moodboard or group on screen and only for the page of about 120 cards shown.

## Likes

The own feed exposes `image_likes`, and the archive UI has a Liked filter. `likes.jsonl` records this field for selected own jobs when nonempty. It is not a crawl of liked work by other creators. A separate all-account liked-items endpoint has not been verified.

## Supplied but not used / not verified

- GET `/api/explore-user-jobs` with `username_v2`, `page_size`, `cursor`, `checkpoint`: supplied in the request; the exporter instead uses the site's observed own archive feed.
- POST `/api/job-status`: supplied in the request; request-body shape and ownership guarantees were not verified, so the exporter does not call it.
- A job detail was opened from the own archive. Its data was already available from that feed; no independent job-detail endpoint was needed or established.

## Safety boundaries

Only read operations are implemented. No generation, ranking submission, edit, deletion, settings, billing, or subscription endpoint is called. Metadata requests are serialized at least one second apart. No timers initiate a new run, no alarms exist, and no remote code or telemetry is used. The site's own unrelated background requests are outside extension control and are not replayed.

## Small-test confirmations

The 20-job-capped metadata test reached an empty next page after 18 jobs. Successful GETs confirmed the exporter header and request construction. One `event_type: variation` record points to another selected job, with `parent_grid: 0`; all parent IDs resolve within the selected data. The custom profile exposed its code through a selected job reference. The selected board contains 23 CDN PNG URLs.
