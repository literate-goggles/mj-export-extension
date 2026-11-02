# What an MJ Export export contains

This page describes the files the MJ Export extension writes, the Midjourney web endpoints they come from, and how their parts relate. It is derived only from this repository's code and docs at extension version 0.3.1, except [The designer's selection](#the-designers-selection), which describes what a later version adds when the designer chooses what to export. Citations are `path:line` from the repository root. Raw endpoint observations are in [docs/ENDPOINTS.md](../docs/ENDPOINTS.md) and are not repeated here. The statuses verified, inferred and unsupported are defined in [section 4](#4-verified-inferred-unsupported).

Versions 0.3.0 and 0.3.1 write the same format: 0.3.1 (commit `83b3540`) changed no writer code, only the UI defaults (All jobs, images on, Small test off) and when saved settings are restored. No 0.2.0 code is in Git; that version is known only from `docs/TESTING.md:3-11`.

## 1. Midjourney endpoints

The exporter sends four kinds of metadata request. All are same-origin GETs to `https://www.midjourney.com`, issued by the page bridge from inside the signed-in page (`src/page-bridge.js:4,56`). Only `/api/imagine` paginates.

| Request | Query | What the exporter keeps | Evidence |
|---|---|---|---|
| GET `/api/profile` | `user_id` (own id) | `user_id`, `username_v2`. The response must echo the own id ("Account identity mismatch"), and both fields must be present. | `src/page-bridge.js:18-20,67`; `src/content.js:38-39` |
| GET `/api/imagine` | `user_id`, `page_size`; later pages add `cursor`, `checkpoint` | `cursor`, `checkpoint`, and per job `id`, `enqueue_time`, `full_command`, `width`, `height`, `batch_size`, `job_type`, `event_type`, `parent_id`, `parent_grid`, `parent_index`, `image_likes`, `personalization_codes`, `video_segments`. A `data` that is not an array, or any `id` that does not match `^[a-f0-9-]{36}$` (any case), stops the run. | `src/page-bridge.js:7,10-16,68` |
| GET `/api/moodboards` | none | Boards whose `user_id` is the own id (others are dropped silently): `moodboard_id`, `title`, `created`, `user_id`, `version`, `code`, and per image `url`, `width`, `height`, `created`. A response that is not an array stops the run. | `src/page-bridge.js:22-27,69` |
| GET `/api/personalized-profiles` | none | Profiles whose `user_id` is the own id: `id`, `created`, `majorVersion`, `rankingCount`, `service`, `title`, `type`, `user_id`, `code`. From `counts`: `main`, `mainV7`, `niji`, `nijiV7`, `profileCounts`. | `src/page-bridge.js:29,70` |

Not everything kept is written. `batch_size` only sizes the image list and decides the kind, `video_segments` becomes a boolean, and each board image's `created`, the `user_id` of boards and profiles, and `counts.profileCounts` are dropped (`lib/normalize.js:9-10,26-43`). The full lists of observed fields, including those the bridge never keeps, are in `docs/ENDPOINTS.md:23,29-35`.

### Pagination of `/api/imagine`

- Page size is 100 in All and Since modes, and `min(100, N - jobs so far)` in Max mode. The bridge clamps it to 1-100 (`src/content.js:21`; `src/page-bridge.js:68`). In 0.3.x, the 20 that `docs/ENDPOINTS.md:19` mentions comes from Max N = 20; the Small test option limits boards and profiles, not jobs.
- Each later request sends back the previous response's opaque `cursor` and `checkpoint`. Null or empty values are left out (`src/content.js:22,29`; `src/page-bridge.js:55`).
- The loop runs while `cursor` is truthy. An empty page that still has a cursor does not end it, and there is no page cap (`src/content.js:19-34`). Rows are de-duplicated by `id` across pages (`src/content.js:24`).
- It ends on a null or empty cursor (`feed_exhausted: true`), or when Max N is reached (`feed_exhausted` then says whether that page's cursor was empty). A cursor seen before stops the run with "Pagination cursor repeated; stopped to avoid looping" (`src/content.js:31-35`).
- No order is assumed for dates. Since mode reads the whole feed and keeps jobs whose `enqueue_time` is on or after the chosen date at 00:00 UTC. An absent or unparseable time is kept; an explicit null counts as 1970 and is dropped (`src/content.js:25`). Max mode keeps the first N jobs in feed order (`src/content.js:26`).
- Every attempt, a resume included, starts again at the first page. The saved `cursor`, `checkpoint` and `discovered_ids` are written but never read (`src/content.js:18,30`; `docs/ARCHITECTURE.md:13`).
- Live runs have shown one page followed by an empty page with null `cursor` and `checkpoint`. The site's own next-page request sent `cursor` without `checkpoint` (`docs/ENDPOINTS.md:19,59`).

### Request rules

- Requests are made only while an own-work page (`/organize`, `/imagine`, `/personalize` or `/moodboards`) is open, with `credentials: 'same-origin'`, the header `x-csrf-protection: 1` and `redirect: 'error'`. The extension reads no cookies or authentication headers (`src/page-bridge.js:2,52,56`; `docs/ARCHITECTURE.md:5`).
- The own account is the `user_id` of the site's own `/api/imagine` request. Boards and profiles are filtered by it, and `/api/profile` must echo it. Job rows are not checked one by one: they count as own because the feed was requested with the own id (`src/page-bridge.js:7,19,24,29,42`).
- Metadata requests are serialized at least 1 s apart, in both the bridge and the scheduler (`src/page-bridge.js:54`; `lib/transport.js:9`).
- HTTP 403 (when no challenge was detected), 429 and 5xx get 3 attempts in total. Between attempts the scheduler waits `max(2 s * 2^n, Retry-After)`, with Retry-After capped at 120 s, so 2 s and then 4 s when the header is absent (`lib/transport.js:3,16-18`; `tests/transport.test.js:8-13`).
- These stop the run at once: 401 ("Session expired"); challenge text on the page (a human check, unusual traffic, a suspended or banned account, a captcha) or an OK response that is not JSON ("Site challenge or warning"); any other status; the third retryable failure; a bridge error (unexpected shape, identity mismatch, not an own page); a network exception, a redirect included; a page read that takes over 90 s; and leaving the own page (`lib/transport.js:12-17`; `src/page-bridge.js:49,52-53,59`; `src/content.js:4,14`).
- The bridge also records the key names, value types and array lengths, never the values, of the site's own responses on these paths, for the panel's "API field names" view. None of it is written to the export (`src/page-bridge.js:31-36,43`).

### Image downloads

- The API does not list job image URLs. The exporter builds `image_urls` as `https://cdn.midjourney.com/<job_id>/0_<i>.jpeg` for i from 0 to `min(batch_size, 4) - 1`, with one slot when `batch_size` is not a positive integer (`lib/normalize.js:9-10`). Board images use each URL exactly as returned (`src/content.js:111`).
- Files are saved with `chrome.downloads`, byte for byte, overwriting the same path (`src/service-worker.js:8-16`; `README.md:57`). Only `https://cdn.midjourney.com/` URLs that the bridge allowlisted from own records are fetched: job URLs built from own job ids (`.jpeg`, `.png`, `.webp`), and board URLs as returned (`src/page-bridge.js:14,25,71`; `src/content.js:55,59`; `src/service-worker.js:10`). A board image on another host is not fetched: it gets a failure of kind `image`, and its `file` stays null.
- Chrome reports error categories, not HTTP statuses. The worker maps FORBIDDEN and UNAUTHORIZED to 403 and SERVER_FAILED and NETWORK to 503, which are retried as above; BAD_CONTENT to 404; and anything else to a stop (`src/service-worker.js:30`).
- A 404 on a URL ending in `/0_<i>.jpeg`, the job URL shape, is retried once with `.png`. Any other 404 stops the run, so board URLs as observed have no fallback, and `.webp` is allowlisted but never requested (`src/content.js:61-66`; `src/page-bridge.js:14`).
- At most two downloads run at once, and they skip the 1 s spacing (`lib/transport.js:22-29`; `src/content.js:58`).

### Endpoints not used

- Observed but not used: GET `/api/imagine-update` (the site's live refresh by `checkpoint`), and CDN thumbnails ending in `_384_N.webp` (`docs/ENDPOINTS.md:12,41`).
- Supplied but never verified or called: GET `/api/explore-user-jobs` and POST `/api/job-status`. No job-detail endpoint was established, and no account-wide liked-items endpoint was verified (`docs/ENDPOINTS.md:47,51-53`).

## 2. Export layout

The extension writes a folder under Chrome's download directory, never a ZIP (`README.md:3,51,62`). Any ZIP of it was made by hand afterwards, so its top folder and extra entries vary.

```text
mj_export_<username>_<YYYYMMDD>[_<epoch ms>]/
  export_meta.json   run summary, written last, at the end or stop of every attempt
  jobs.jsonl         one row per selected job
  moodboards.jsonl   one row per own board (only the first one with Small test)
  profiles.jsonl     custom profiles, then synthesized global profiles
  likes.jsonl        one row per selected job with non-empty image_likes
  images/            <job_id>_<i>.<ext> and moodboard_<board_id>_<i>.<ext>
```

`<username>` is `username_v2` with every character outside `[A-Za-z0-9_.-]` replaced by `_`. `<YYYYMMDD>` is the UTC date when the run was created, and New export adds `_<epoch ms>` (`src/content.js:90`). Each JSONL row is one `JSON.stringify` object followed by a newline, so an empty table is a 0-byte file (`src/content.js:75`). `export_meta.json` is indented JSON (`src/content.js:76`). A value that was `undefined` is written as a missing key, so `feed_exhausted`, a board's `name` or a custom profile's fields can be absent rather than null (`lib/normalize.js:28,38`; `src/content.js:73`). `scripts/verify-export.py` checks a folder offline.

### export_meta.json

Evidence: `src/content.js:12,16,55,73,86,90,99,102,114,116`; `lib/normalize.js:46`.

| Field | Content |
|---|---|
| `extension_version` | The manifest version. |
| `export_time` | ISO UTC with milliseconds, taken at this write: the end or stop of the latest attempt, not its start. |
| `username` | `username_v2`. The account id itself is not recorded here. |
| `scope` | `{mode, max, since, images, testSubset}` of the latest attempt; `mode` is `all`, `since` or `max`. `max` is always present (20 by default) but applies only in Max mode, and `since` is a `YYYY-MM-DD` date or `''`. |
| `status` | `complete` (the latest attempt had no failures), `completed_with_failures` (only video jobs and board images on other hosts cause it), `paused` or `stopped`. The in-memory states `reading` and `exporting` are never written. |
| `counts` | `jobs`, `moodboards`, `profiles`: rows written. `likes`: jobs with likes. `files`, `download_bytes`: every image download record of the run, across attempts, and Chrome's sizes for them. `completed_jobs`: jobs that finished the job loop (video jobs never do). |
| `failures` | `[{kind, id, reason}]` from the latest attempt only. `image`: the id is the requested `images/...jpeg` path. `job`: a video job. `run`: id `export`, with the error that stopped or paused the run. |
| `parent_gaps` | `[{job_id, parent_id, reason}]` for every job whose `parent_id` is not an exported `job_id`, recomputed at each write, with one fixed reason. With a selection, a parent that was in scope but not exported because of it also has `left_out: true`, and its reason reads "The designer left the parent out of this export". |
| `feed_exhausted` | True when the last page read had no cursor. |
| `limitations` | Three fixed sentences, plus "Items the designer left out are not in this export." with a selection. |
| `selection` | Absent from older exports, null when the designer did not choose, otherwise what they chose: see [The designer's selection](#the-designers-selection). |

### jobs.jsonl

Keys are written in this order (`lib/normalize.js:10`; `lib/parser.js:17-30`):

| Field | Content |
|---|---|
| `job_id` | The API `id`. |
| `created_at` | `enqueue_time` as returned, or null. |
| `prompt` | `full_command` up to the first top-level `--flag`, trimmed. |
| `full_command` | Verbatim. |
| `flags` | `ar`, `v`, `stylize`, `chaos`, `weird`, `niji`, `iw`: the value as a string, `true` for a bare flag, else null; the last occurrence wins. `sref`, `profile`: token lists. `raw`, `tile`: booleans. Accepted aliases: `aspect`, `version`, `s`, `c`, `w`, `p`. |
| `profile_default` | True when `--p` or `--profile` had no value. |
| `unknown_flags` | `{name: [value or true, ...]}` for every other flag. |
| `parent_id` | The API `parent_id`, or null. |
| `parent_index` | The API `parent_index` when it is an integer, else `parent_grid` when that is, else null. |
| `kind` | See section 3. |
| `image_files` | Relative paths of the saved images; `[]` when images were off or none were saved. |
| `image_urls` | The built CDN URLs, each replaced by the URL actually downloaded (`src/content.js:105`). |
| `width`, `height` | The job's API dimensions: metadata, not measured. |
| `job_type`, `event_type` | As returned, or null. |
| `parent_grid` | The raw API value, or null. |
| `personalization_codes` | The API's reference array, unchanged, or `[]`. |
| `image_likes` | The API array, unchanged, or `[]`. |
| `video` | `!!video_segments`, so true even for an empty array. |

### moodboards.jsonl

Evidence: `lib/normalize.js:12-34`; `src/content.js:110-113`; `docs/ENDPOINTS.md:29`.

| Field | Content |
|---|---|
| `id` | `moodboard_id`. Observed as long digit strings: compare them as strings. |
| `code`, `codes` | `codes` holds the API `code` if present (never observed), then the codes of exported job references whose `id` equals the board id, unique, in feed order. `code` is `codes[0]`, or null. |
| `name` | `title`. |
| `version` | `version`, or null. Never observed. |
| `created_at` | `created`, or null. Observed as `YYYY-MM-DD HH:MM:SS.ffffff` with no time zone. |
| `generated_job_ids` | The exported jobs that have a reference whose `id` equals the board id, in feed order. |
| `generated_left_out` | Only with a selection: how many of the board's generations in scope were not exported because of it (left out, or appeared after choosing). Written as 0 too. |
| `images` | The board's current reference images in API order, as `{source_url, file, width, height}`. `file` is set only after a successful download. |

### profiles.jsonl

- **Custom profiles** are API records, in API order: `{id, codes, name: title, version: majorVersion, created_at: created, ratings_count: rankingCount, service, type}`. A field the API leaves out is absent. Observed: `created_at` in epoch milliseconds, `version` as a string, `type` as `profile` (`lib/normalize.js:38`).
- **Global profiles** are synthesized. One is added for each of `counts.main`, `mainV7`, `niji` and `nijiV7` that is a number, unless a profile with that id already exists: `{id: '<own user_id>_<6|7>_<main|niji>', codes, name, version: '6' or '7', ratings_count, service, type: 'global'}`, named `Global V6 Profile`, `Global V7 Profile`, `Global Niji V6 Profile` or `Global Niji V7 Profile`, with no `created_at` key (`lib/normalize.js:39-43`).
- `codes` are the codes of exported job references whose `id` equals the profile id, plus, on custom profiles, the API `code` (never observed), unique (`lib/normalize.js:37-38,42`).

### likes.jsonl and images/

- `likes.jsonl` holds `{job_id, image_likes}` for each selected job whose `image_likes` is non-empty. The same arrays stay on the job rows (`src/content.js:72`).
- A job image is `images/<job_id>_<i>.jpeg`, or `.png` after the PNG fallback, where `<i>` is the 0-based image slot (`src/content.js:57,62,104`).
- A board image is `images/moodboard_<board id>_<i>.<ext>`, where `<i>` is its position in the board's `images`, and `<ext>` is the URL path's suffix when that is `png`, `jpg`, `jpeg` or `webp` (lower-cased), else `jpeg` whatever the bytes are (`src/content.js:57,111`).
- The suffix therefore comes from the URL, not from the bytes or the MIME type, although `docs/ENDPOINTS.md:43` says the MIME type decides. Check magic bytes, as `scripts/verify-export.py:15-40,62` does.
- Match files to slots by the `_<i>` in the name. A partial run can leave `image_files` shorter than `image_urls`, with the saved slots kept in order (`src/content.js:105-106`).

### Resumed, partial and interrupted runs

- **Export / resume** continues the saved run in the same folder. It refuses another account, and any scope change except Include images. It then re-reads the identity, the feed from the first page, the boards and the profiles; keeps earlier job `image_files`; skips downloads that Chrome still reports complete; overwrites unfinished files; clears `failures`; and rewrites every metadata file when it ends (`src/content.js:53-54,84-99,114`; `README.md:66`).
- **New export** starts over in a new `_<epoch ms>` folder and leaves earlier folders alone (`src/content.js:90`; `README.md:34`).
- **Paused or stopped** runs still write metadata, with a `run` failure (`Run paused`, or the error that stopped the run). Jobs can have fewer `image_files` than `image_urls`, and `completed_jobs` can be below `jobs`. Board `file` values stay null until that attempt reaches the boards, even for files an earlier attempt saved, so those files look unreferenced (`src/content.js:96,110-116`; `tests/content.test.js:78-83`).
- **Stopped while reading**: the rows and `feed_exhausted` are the previous attempt's. A new run writes empty files and no `feed_exhausted` (`src/content.js:90-99`).
- **Metadata only** (`images: false`): `image_files` is `[]`, `file` is null, and `image_urls` are unchecked guesses. A metadata-only resume after an image run keeps the job `image_files`, resets board `file` to null and keeps `counts.files` (`src/content.js:95-96,110`).
- **No `export_meta.json`**: the first attempt is still running, or it was killed (a closed tab, a browser crash) before it ended. A killed resume leaves the previous attempt's metadata beside newer images (`src/content.js:70-76`; `docs/ARCHITECTURE.md:21`).

### The designer's selection

A later extension version lets the designer choose what to export: whole moodboards in or out, and single generations in or out. The choice is saved once and used for the whole run, so a generation that appears in scope after it was made is not exported either; it counts as "not reviewed". What was not exported is absent from the files:

- A left-out or not-reviewed job has no `jobs.jsonl` or `likes.jsonl` row and no downloaded image, and no board's `generated_job_ids` lists it.
- A left-out board has no `moodboards.jsonl` row and no downloaded reference image.
- Profile `codes` come from exported jobs only.

`export_meta.selection` records the choice:

| Field | Content |
|---|---|
| `chosen_at` | When the designer chose, ISO 8601 UTC. |
| `moodboards` | `{available, exported, left_out}`: the boards `/api/moodboards` returned at export time, the boards exported, and the boards the designer unchecked. |
| `jobs` | `{available, exported, left_out, not_reviewed}`: the jobs in the scope (mode, maximum, since date) at export time, the jobs exported, the jobs the designer unchecked, and the jobs in scope that the saved choice does not cover. |
| `left_out_moodboard_ids` | Only the left-out boards that at least one exported job references by a personalization reference id, so that readers can label those references. Other left-out boards are only counted. |

`exported + left_out + not_reviewed` can be less than `available`: with Small test, for example, fewer boards can be exported than were chosen. Do not expect the numbers to add up. `counts` still describe what was exported, and job rows keep their shape and the API data as returned, so an exported job's `personalization_codes` and `full_command` can still name a left-out board. With a selection, every board row also has `generated_left_out`, and `parent_gaps` marks the parents the designer left out (see the tables above).

## 3. How the parts relate

```text
job.parent_id -------------------> job.job_id                (parent_index = image slot in the parent)
job.personalization_codes[].id --> moodboard.id, profile.id  (by id only, whatever the type)
moodboard.generated_job_ids[] ---> job.job_id                (the exporter's copy of the link above)
moodboard.codes, profile.codes <-- personalization_codes[].code of those references
likes.jsonl job_id --------------> job.job_id
image_files[], images[].file ----> files in images/
```

- **Lineage.** `parent_id` is the only lineage link; the feed's selection and shown flags are never used (`README.md:82`). `parent_index` names the parent's image slot, 0-based, which is inferred from the field names and one observed variation with `parent_grid: 0` (`docs/ENDPOINTS.md:25,59`). `parent_grid` is kept raw, and when neither field is an integer, `parent_index` is null. No child list is written, so children are found by reverse lookup. A parent outside the export is listed in `parent_gaps` and is never fetched; "outside the scope" and "unavailable" share one reason (`lib/normalize.js:46`; `README.md:81`), and only a parent the designer left out is told apart, by `left_out: true`. Max and Since scopes can cut chains, and so can a selection. Chains longer than one step, and cycles, have not been observed.
- **Kind.** The lower-cased `job_type + ' ' + event_type` is matched against `zoom`, `pan`, `remix`, `upscale|upsample` and `variation|vary`, and the first match wins. Otherwise a parentless job with `batch_size` > 1 is `grid`, and anything else is `other` (`lib/normalize.js:4-7`). These are substring heuristics: `pan` also matches `expand`. Only `grid` and `variation` have been observed, and the observed variation was itself a four-image job (`docs/TESTING.md:5` counts 18 jobs and 72 job images, at most 4 per job by `lib/normalize.js:9`). `batch_size` is not exported, so the grid rule can only be re-checked through the number of `image_urls`.
- **Personalization references.** Each entry of `personalization_codes` has `code`, `id`, `type` and `user_id` (`docs/ENDPOINTS.md:25`). The types observed in a sample export are `moodboard`, `profile` and `user`, and the `user` entries had no `id`. The repository does not say what the types mean. The exporter links by `id` alone, for any type, and skips entries without one (`lib/normalize.js:16,24,37`).
- **Moodboards.** `images` and `generated_job_ids` are separate datasets: the board's current pictures, and the exported jobs that reference it (`docs/ARCHITECTURE.md:15`). The rows and the links come from one job set, so in an unmodified 0.3.x export every listed id is an exported job, and every exported job that references the board is listed. An empty list means no exported job references the board, not that it was never used (`README.md:13`). With a selection, `generated_left_out` counts the board's generations that were not exported because of it. The current pictures may differ from the ones a job was made with.
- **Codes.** The API has exposed no board or profile code, so `codes` are recovered from the references whose `id` matches. Several codes mean different codes were seen on different jobs (`README.md:13` calls them historical). A record that no exported job references has `codes: []` and `code: null`. Codes are never made up from ids (`docs/ENDPOINTS.md:29`; `lib/normalize.js:25,37-38`).
- **Profiles.** Custom profiles are API records. Global profiles are rows the exporter builds from aggregate counts, and their `ratings_count` is a count, not ratings (`README.md:55`). There is no profile-to-jobs list: a job reaches a profile only through a reference id. A bare `--p` (`profile_default`) names no profile, and the codes in `flags.profile` are plain text. In a sample export, a job's `--profile` code equalled its reference code.
- **Likes.** `image_likes` is copied raw onto every job, and `likes.jsonl` repeats the non-empty ones. Both cover own selected jobs only (`README.md:85`; `docs/ENDPOINTS.md:47`).

### How the viewer labels links

The viewer ([README](README.md#how-links-are-labelled)) keeps the exporter's id rule and marks everything else (`viewer/mjviewer/model.py:4-22`, function `_link`):

| Label | When | Why |
|---|---|---|
| `exporter` | The board's `generated_job_ids` lists the job. | It is the exporter's own link. |
| `derived` | A job reference's `id` equals the board id, but the row has no usable `generated_job_ids` (an older writer), or the list leaves the job out (also a warning). | It is the exporter's rule, recomputed. In an unmodified 0.3.x export it adds nothing. |
| `explicit` | A job reference's `id` equals the profile id. In a job's own reference list, `explicit` marks any reference whose `id` equals an exported board or profile id, whatever its `type`. | It is the exporter's rule. |
| `inferred_code` | No id matches, but a reference code or a `--profile` flag code is one of a board's or profile's `codes`. | It is likely but not proven: the exporter never matches by code, and what a code identifies over time is undocumented. |
| `left_out` | No exported record has the reference's `id`, but `selection.left_out_moodboard_ids` lists it. | The designer left that board out, so there is nothing to link to. The id names the board, so a matching code is not used, and the reference is not counted as unresolved. |
| `unresolved` | Nothing matches, for example a `user` reference with a code and no id. | There is nothing in the export to link to. The code stays visible and can be filtered. |

A parent that `parent_gaps` marks `left_out` is shown as left out by the designer, not as a gap of the export.

## 4. Verified, inferred, unsupported

**Verified**: the code does it, a repository test checks it, or a live run recorded in `docs/` showed it. **Inferred**: plausible from field names, a code path never run live, or a single observation. **Unsupported**: not exported, never verified, or contradicted by the code.

| Claim | Status | Evidence |
|---|---|---|
| The exporter reads exactly four endpoints, GET only, through a fixed allowlist. | verified | `src/page-bridge.js:4,56,67-72` |
| `/api/imagine` returns `{data, cursor, checkpoint}`, and an empty page with a null cursor ends the feed. | verified | `docs/ENDPOINTS.md:11,19,59`; `docs/TESTING.md:5` |
| Several non-empty pages are followed correctly, and `checkpoint` may be sent with `cursor`. | inferred | never seen live; the site sent `cursor` alone (`docs/ENDPOINTS.md:19`) |
| `/api/moodboards` and `/api/personalized-profiles` return everything in one response. | inferred | no pagination keys observed (`docs/ENDPOINTS.md:14-15`) |
| The feed is newest first. | inferred | observed in a sample export; only Max mode and the order of `generated_job_ids` and `codes` depend on it |
| Job images are at `https://cdn.midjourney.com/<job_id>/0_<i>.jpeg`. | verified | `docs/ENDPOINTS.md:41`; `docs/TESTING.md:5` |
| The `.png` fallback for job images works. | inferred | the code path has never run live (`src/content.js:61-64`) |
| Board references are CDN PNG URLs. | verified for the boards seen | `docs/ENDPOINTS.md:59`; other hosts become failures (`src/content.js:55`) |
| The MIME type decides the local file suffix. | unsupported | the code takes the URL's suffix (`src/content.js:57`), contrary to `docs/ENDPOINTS.md:43` |
| A variation links to its grid by `parent_id`, with an integer `parent_grid`. | verified | `docs/ENDPOINTS.md:59`; `lib/normalize.js:8` |
| `parent_index` is the 0-based image slot in the parent. | inferred | field names and one observation |
| The kinds `upscale`, `zoom`, `pan` and `remix`. | inferred | keyword rules, never observed (`lib/normalize.js:6`) |
| `generated_job_ids` lists the selected jobs whose reference id equals the board id, in feed order. | verified | `lib/normalize.js:12-29`; `tests/normalize.test.js:13-23` |
| Board and custom-profile codes come from job references. | verified | `docs/ENDPOINTS.md:29,59`; `lib/normalize.js:25,37-38` |
| Several codes on one board are historical codes. | inferred | `README.md:13` |
| Global profiles are built from four counts, with id `<user_id>_<major>_<service>`. | verified | `lib/normalize.js:39-43`; `tests/normalize.test.js:8-11` |
| That id format is the site's own. | inferred | the site's UI showed it (`docs/ENDPOINTS.md:37`) |
| Boards and profiles carry an API `code`, and boards a `version`. | unsupported | picked when present, never observed (`src/page-bridge.js:26,29`; `docs/ENDPOINTS.md:29`) |
| What reference `type` means, and why `user` entries have no `id`. | unsupported | undocumented; observed in a sample export only |
| The shape of the elements of `image_likes`. | unsupported | never observed non-empty (`docs/ENDPOINTS.md:47`) |
| `complete` means the latest attempt had no failures; `reading` and `exporting` are never written. | verified | `src/content.js:114,116` |
| Pausing during board downloads cannot report `complete`. | verified | `tests/content.test.js:78-83` |
| A resume keeps completed image files. | verified (0.2.0, live) | `docs/TESTING.md:7`; `src/content.js:53-54` |
| Image dimensions match the API's. | verified for the live sample | `docs/TESTING.md:5`; rows carry API values, not measurements (`src/content.js:73`) |
| The account is the one whose feed the site requested. | inferred | `/api/profile` only echoes that id (`src/page-bridge.js:19,42`) |
| `username` can be an id-like value rather than a handle. | inferred | observed in a sample export |
| Exports from 0.3.0 and 0.3.1 have the same format. | verified | 0.3.1 (`83b3540`) changes no writer code |
| The 0.2.0 format. | unsupported | no 0.2.0 code in Git (`docs/TESTING.md:3-11`) |
| Full-history, multi-account or very large exports, and live rate limits. | unsupported | untested (`docs/TESTING.md:13`; `docs/ARCHITECTURE.md:21`) |

## 5. Known gaps

What the exporter cannot export:

- Videos, which become a `job` failure, so such an export is never `complete`; and deleted, trashed, hidden or legacy jobs (`src/content.js:102`; `README.md:11,86`).
- Earlier versions of a board, deleted boards, a board's use beyond the exported jobs, and each board image's `created` time, which is fetched and dropped (`README.md:12-13`; `lib/normalize.js:30-32`).
- Personalization weights and individual ratings; `counts.profileCounts`, fetched but never written; the `*Btrees*` counts, never fetched; and any global profile other than V6, V7, Niji V6 and Niji V7, since only those four count keys are read (`README.md:14`; `src/page-bridge.js:29`; `lib/normalize.js:39`; `docs/ENDPOINTS.md:35`).
- Likes of other creators' work (`README.md:85`).
- What the designer left out, by choice. Only its numbers, and the ids of left-out boards that exported jobs use, are recorded (`export_meta.selection`).
- Fields the site returns but the exporter drops: on jobs `batch_size`, `rating`, `published`, `shown`, `user_hidden`, `template` and `image_hides`, and on boards `personalize` (`src/page-bridge.js:7,26`; `docs/ENDPOINTS.md:23,29`).
- The account id in `export_meta.json`. It appears only as the references' `user_id` and inside global profile ids.

What is ambiguous:

- The element shape of `image_likes`; what the reference types mean; whether boards or profiles ever carry an API `code`, or boards a `version`.
- Dates come in several forms. Jobs carry ISO strings with an offset, board `created_at` has no time zone, custom-profile `created_at` is a number, and global profiles have no date (observed in a sample export).
- When a board or profile has several codes, which one a job used is known only from that job's reference.
- `parent_index` beyond the one observed variation, and what a non-integer `parent_grid` would mean.
- `counts.files` and `download_bytes` count the download records of the whole run, so after resumes they can exceed the files that rows reference (`src/content.js:73`).
- Scope: Max and Since exports can omit parents and board generations, and Small test keeps only the first board and the first profile, where custom profiles come first (`src/content.js:96-97`).
- Command parsing: only the listed aliases are known, so `--personalize`, `--style`, `--seed`, `--no`, `--sw`, `--sv` and `--oref` go to `unknown_flags`. Values are strings, so `7` and `7.0` differ. Image-prompt URLs stay in `prompt`. A quote opened at the start or after whitespace that never closes hides every later flag, leaving `prompt` equal to `full_command` (`lib/parser.js:11,18,27`; `README.md:87`).
- Ids: board ids were observed as long digit strings, and job ids are checked only against a pattern. Keep every id a string (`src/page-bridge.js:13`).
