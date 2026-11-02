# MJ Export Viewer HTTP API (api 1)

The local server in `mjviewer/server.py` serves the single-page UI and this JSON
API. Everything is read-only with respect to exports: ZIP members are streamed,
never extracted; folder files are opened read-only through validated paths.

Sections marked **Deviation** differ from the original api 1 contract. Every
deviation is additive or stricter than the contract; none removes a field.

The [hosted mode](#hosted-mode) (`--hosted`) serves the same routes behind a
Google sign-in, with exports from a published catalog; its differences are
listed at the end, after the [`--check`](#command-line-check) command.

## Security

| Rule | Behaviour |
|---|---|
| Bind address | `127.0.0.1` unless `--host` is given. |
| Host header | Must name `localhost`, `127.0.0.1` or `[::1]` (any port), or an `--allow-host` name; otherwise `403 forbidden_host`. A missing Host header is rejected too. Applies to every route, including `/` and `/static/*`. |
| Session secret | Created at start (`secrets.token_urlsafe(32)`). `GET /` replaces every `{{MJV_TOKEN}}` in `static/index.html` with it and sets `mjv_<port>=<secret>; HttpOnly; SameSite=Strict; Path=/`. |
| `/api/*`, `/files/*` | Need that cookie (`403 forbidden` otherwise). Comparison is constant-time. |
| Non-GET requests | Also need `X-MJV-Token: <secret>` (`403 forbidden`). |
| `/static/*` | Needs neither cookie nor token. Paths are validated and must resolve inside the static folder. |
| Headers on every response | `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cross-Origin-Resource-Policy: same-origin`. API JSON also gets `Cache-Control: no-store`. |
| Errors | `{"error": {"code": "...", "message": "..."}}` with a fitting status, including malformed requests and unsupported methods. |
| Logging | Only failed requests (status 400 or higher) and non-GET requests are logged, as method, path and status. Query strings are never logged, because they can contain prompt search terms. A client that closes or resets its connection (a closed tab, a dropped keep-alive) is not an error and is not logged. |

**Deviation (stricter):**

- A non-GET request with an `Origin` header that is not `http://<Host>` gets `403 forbidden_origin`. Requests without `Origin` (curl, tests) are accepted when the cookie and token are valid.
- Request targets must be in origin form (`/path?query`). Absolute-form targets and `*` get `400 bad_request`. A leading `//` is never routed to the API.
- Methods other than GET and POST get `405 method_not_allowed`, also as JSON.
- Every non-GET response closes the connection, and so does the answer to any request that declares a body (`Transfer-Encoding`, or a `Content-Length` other than 0), GET included, so an unread request body is never parsed as the next request.

## Pages

| Route | Response |
|---|---|
| `GET /` (also `/index.html`) | `static/index.html` with the secret injected; `Cache-Control: no-store`. `503 ui_missing` when the file is absent. |
| `GET /static/<path>` | The file, with a type from its extension, and `Cache-Control: no-cache`. |
| `GET /favicon.ico` | `204` (no body). |
| `GET /<anything without a dot in its last segment>` | Same as `GET /`, so the UI can use client-side routes. **Deviation (additive).** |

## Endpoints

### `GET /api/health`

`{ok: true, api: 1, version, thumbnails: bool, max_upload_bytes: int}`

`thumbnails` is true when thumbnails are enabled and Pillow imports.
**Deviation (additive):** `max_upload_bytes` lets the UI check a file's size before it uploads.

### `GET /api/exports`

`{exports: [ExportSummary], library: [{path, name, kind: 'zip'|'folder', opened: bool}], failed: [{path, root?, code, message}]}`

- `library` lists the top-level `*.zip` files and the export-looking folders of each `--library DIR`, rescanned on every call. A folder qualifies when it, or one of its direct subfolders, holds `export_meta.json` or `jobs.jsonl`.
- **Deviation (additive):** `failed` lists command-line paths that could not be opened, and export roots that could not be read while the other roots of the same ZIP or folder opened (then with `root`, the root's path inside it). Each is listed once.

`ExportSummary`:

| Field | Meaning |
|---|---|
| `id` | 12 hex characters derived from the source. For a ZIP: real path, size, mtime and root. For a folder: real path and root. Stable across restarts. Opening the same root again returns the same export. |
| `label` | Last segment of the root, or the source name without `.zip` when the root is the archive top. |
| `source` | `{type: 'zip'|'folder'|'upload', name, path}`. For an upload, `name` is the sanitized `X-Filename` and `path` is the cached copy. |
| `root` | Path of the export root inside the ZIP or folder (`''` at the top). |
| `extension_version` | From `export_meta.json`, or null. |
| `version_support` | `supported` for 0.3.0 and 0.3.1 (one writer, checked against `lib/normalize.js` and `src/content.js`), `unknown` for any other version (0.2.x included; read with best effort), or `missing`. |
| `export_time` | Normalized ISO 8601 UTC string (`YYYY-MM-DDTHH:MM:SS.mmmZ`), or null. |
| `username`, `status` | As exported, or null. |
| `counts` | `{jobs, moodboards, profiles, likes, images_expected, images_present}`, observed in the files and not copied from the metadata. |
| `warnings`, `errors` | Number of checks at that level. |

### `POST /api/exports/open`

Body `{"path": "<zip or folder>"}`; `~` is expanded. Returns `{exports: [ExportSummary], failed: [{path, root, code, message}]}`. A ZIP or folder holding several export roots opens each of them; a root that cannot be read does not stop the others (**deviation, additive:** it is listed in `failed`, here and in `GET /api/exports`). Only when no root can be read does the open fail, with `load_failed`.

Export roots: every directory that contains `jobs.jsonl` or `export_meta.json`, at any depth in a ZIP and up to 3 levels deep in a folder. `__MACOSX/`, AppleDouble `._*` files and `.DS_Store` are ignored.

| Error code | When |
|---|---|
| `404 not_found` | Nothing exists at the path. |
| `403 unreadable` | The file cannot be read (for example its permissions). |
| `400 not_a_zip` | The path is a file that is not a ZIP. |
| `400 bad_zip` | The ZIP is corrupt, truncated or inconsistent. A file that starts like a ZIP but has no end record (an interrupted copy or download) is reported as truncated. |
| `400 too_many_entries` | More than 500,000 entries, or a central directory over 128 MiB. |
| `400 no_export_root` | No export root was found. |
| `400 cache_inside_export` | The viewer's cache directory is inside the folder. Opening it would let the viewer write into an export. |
| `400 load_failed` | Any unexpected parse failure in every root. The server keeps running. A value nothing anticipated inside one row skips that row only (`parse_errors`). |
| `400 bad_request` | The body is not `{"path": "..."}`. |

### `POST /api/exports/upload`

The raw ZIP is the body. It needs a `Content-Length`, and `X-Filename` (percent-encoded) for display. The body is streamed in 256 KiB chunks to a temporary file in `<cache>/uploads/`, hashed while it streams, then renamed to `<sha256>.zip`. It is never held in memory. Returns `{exports: [ExportSummary], failed: [...]}`, as `POST /api/exports/open` does.

Errors:

- `411 length_required`.
- `413 too_large` when the size is over `--max-upload-mb` (default 4096). Nothing is written. Bodies up to 64 MiB over the cap are drained so the client can read the answer.
- `400 empty_upload`.
- `400 not_a_zip`.
- `400 no_export_root`, or any open error from the table above.

On an error, the temporary file is removed. So is the stored copy, unless an open export already uses it. Uploading the same bytes twice reuses the stored file and the export.

### `GET /api/exports/{id}/overview`

```
{summary, meta, scope, status, feed_exhausted,
 counts: {meta: {...}|null, observed: {...}},
 failures, parent_gaps, limitations, checks, facets, unresolved, files}
```

Top-level fields:

- `meta` is the raw `export_meta.json` object, or null.
- `scope` is `meta.scope`, or null.
- `status` and `feed_exhausted` come from `meta`.
- `failures`, `parent_gaps` and `limitations` are the metadata lists. They are `[]` when absent or not lists.
- **Deviation (additive):** `selection` is there only when `meta.selection` records that the designer chose what to export: `{chosen_at, moodboards: {available, exported, left_out}, jobs: {available, exported, left_out, not_reviewed}, left_out_moodboard_ids}`. `chosen_at` is normalized ISO UTC, and a value the viewer cannot read is null (an id list `[]`); the `selection_invalid` warning names it. When the designer did not choose (`selection` null or absent) or `meta.selection` is not an object, the key is absent.

`counts.observed` holds:

- `jobs`, `moodboards`, `profiles`, `likes`
- `images_expected`: distinct image paths that are referenced, including unsafe ones (each distinct unsafe value once, however many records reference it)
- `images_present`: files that are servable
- `images_missing`
- `images_unreferenced`
- `bytes_present`
- `images_unsupported`. **Deviation (additive).**

`checks` is a list of `{id, level: 'ok'|'info'|'warning'|'error', title, detail, items}`, sorted by severity. `items` holds at most 500 entries; `detail` says when the list was cut.

| id | Level | Raised when |
|---|---|---|
| `extension_version` | ok / warning | The version is supported, unknown or missing. |
| `export_meta` | error | `export_meta.json` is absent, empty, invalid, too large or unreadable. The exporter writes it last, so its absence means an interrupted run; the rest of the data is still shown. |
| `file:<name>.jsonl` | info / warning / error | That JSONL file is absent, empty, too large or unreadable. |
| `status` | ok / warning | The status is not `complete`: `completed_with_failures`, partial (`paused`, `stopped`), incomplete (`reading`, `exporting`: run states the exporter never writes when a run ends), unknown or missing. |
| `feed` | info / warning | `feed_exhausted` is false (info in `max` mode, where stopping early is expected), or missing or not a boolean while `export_meta.json` exists (warning: the exporter leaves it out when a run stops before the feed was read once). |
| `scope` | info | Always shown when `scope` exists: a plain summary. `max` is mentioned only in `max` mode and `since` only in `since` mode (`jobs created on or after <date> (00:00 UTC)`). |
| `test_subset` | warning | `scope.testSubset` is true: at most one moodboard and one profile were exported. |
| `selection` | info | **Deviation (additive).** The designer chose what to export. The detail says it in plain words, for example `2 of 4 moodboards and 10 of 25 generations. 13 generations and 2 moodboards were left out; 2 generations appeared after choosing and were not exported.` |
| `selection_invalid` | warning | **Deviation (additive).** A value of `meta.selection` or a board's `generated_left_out` is missing or of the wrong type, or `left_out_moodboard_ids` names a board that is in the export. The value is ignored. Items: `{file, field, problem}`, plus `line` and `id` for a board row and `ids` for boards that are in the export. |
| `failures` | ok / warning | The exporter reported failures. |
| `counts` | ok / warning | `meta.counts` differs from the observed data. `files` is compared with `images_present`. |
| `parse_errors` | warning | Skipped lines, as `{file, line, message}`. |
| `duplicates` | warning | Duplicate ids. The first row is kept. |
| `unknown_fields` | info | `{file, field, rows}`. |
| `dates` | info | A date could not be parsed. |
| `files_missing` | warning | A referenced file is absent. |
| `files_unreadable` | warning | A file was refused or unreadable (see below). |
| `files_unreferenced` | info | Files under `images/` that nothing references. |
| `files_unchecked` | info | The export has more than 50,000 files, so types are checked when a file is requested. |
| `metadata_only` | info | Images were off, or no image file is referenced. A metadata-only resume keeps the job files of an earlier run; the detail then says how many are still listed (they are shown normally). |
| `jobs_without_files` | warning / info | A job has no recorded files (stopped or paused runs). |
| `video` | info | Video jobs. Video media is not exported. |
| `unresolved_references` | info | A reference or `--profile` code matches nothing. |
| `references_left_out` | info | **Deviation (additive).** A reference names a moodboard the designer left out (link `left_out`), as `{job_id, type, id, code, source, reason}`. Such references are not unresolved. |
| `generated_job_ids` | ok | Board links are consistent. |
| `generated_job_ids_absent` | info | An older board row has no `generated_job_ids`, so its links are derived. |
| `generated_job_ids_invalid` | warning | `generated_job_ids` is not a list. |
| `generated_job_ids_unresolved` | warning | An id in `generated_job_ids` names a job that is not in the export. |
| `generated_job_ids_omitted` | warning | An explicit id reference is missing from a present `generated_job_ids`. |
| `parent_gaps` | info / warning | A parent is outside the export. The level is a warning when `meta.parent_gaps` does not list the gap. |
| `parent_gaps_left_out` | info | **Deviation (additive).** `meta.parent_gaps` marks a missing parent `left_out: true` (exactly `true`): the designer left it out. Items `{job_id, parent_id, reason}`. These jobs are not counted under `parent_gaps`. |
| `parent_gaps_stale` | warning | `meta.parent_gaps` lists a gap that does not apply. |
| `lineage_cycle` | warning | Parent links form a cycle. |

`facets`:

- `kinds: [{value, count}]`.
- `versions: [{value, label, count}]`. `value` is `v8.2`, `v7`, `niji6`, `niji` (`--niji` without a number) or `unknown`. The order is V by number descending, then Niji, then other values, then Unknown. The source is `--niji`, else `--v` (`7.0` groups with `7`), else a `job_type` shaped like `v8-2_diffusion` or `v7_diffusion`. A `job_type` that mentions niji is never read as a version, because its naming has not been observed.
- `moodboards: [{id, label, count}]`. `count` counts linked jobs of any link type. `label` is the name, else the code, else the id.
- `profiles: [{id, label, count, unresolved}]`. Exported profiles come first. Then each unmatched code follows as `{id: 'code:<code>', label: '<code> (unmatched <type> code)', unresolved: true}`.
- `date_min`, `date_max`: UTC dates (`YYYY-MM-DD`).

`unresolved` holds:

- `references: [{job_id, type, id, code, source: 'reference'|'flag', reason}]`. References to a moodboard the designer left out are not listed.
- `generated_job_ids: [{board_id, job_id}]`.
- `parent_gaps: [{job_id, parent_id, reported_by_exporter, reason}]`: the observed gaps, cross-checked with `meta.parent_gaps`. `reported_by_exporter` is null when `export_meta.json` has no `parent_gaps` list to compare with. Parents the designer left out are not listed.

`files` holds:

- `missing: [{path, referenced_by: [{kind: 'job'|'moodboard', id, index}]}]`.
- `unreferenced: [path]`.
- `unreadable: [{path, reason}]`. This covers unsafe archive names, symlink and special entries, encrypted entries, unsupported compression methods, folder symlinks that leave the root, unsafe referenced paths, and bytes that are not a supported raster type. Hostile names are escaped for display.

### `GET /api/exports/{id}/jobs`

Query parameters:

| Parameter | Meaning |
|---|---|
| `q` | Case-insensitive search over the prompt. Every whitespace-separated term must appear as a substring (Unicode casefold). |
| `kind` | Exact kind. |
| `version` | A facet `value`. |
| `moodboard` | Board id. With `links=explicit`, only `exporter` and `derived` links count. |
| `profile` | Profile id (with `links=explicit`, only `explicit` links count), or `code:<code>`: the jobs on which that code matched nothing, in a reference or a `--profile` flag. These are exactly the jobs the unmatched-code facet counts and the profiles page lists for it; a job that carries the same code in a reference that resolved (by its id, say) is not one of them. |
| `links` | `all` (default) or `explicit`. |
| `date_from`, `date_to` | `YYYY-MM-DD`, inclusive, compared with the job's UTC calendar date. Undated jobs never match a date filter. |
| `sort` | `newest` (default) or `oldest`. Undated jobs come last in both orders; ties sort by job id. |
| `offset` | Default 0. |
| `limit` | Default 60, from 1 to 500. |

An invalid value gets `400 bad_parameter`. Unknown kinds, versions and ids simply match nothing.

Returns `{total, offset, limit, items: [JobCard]}`.

`JobCard`:

```
{job_id, created_at, prompt, kind, kind_source: 'export'|'viewer',
 version: {value, label, source: 'flag'|'job_type'|'none'}, width, height,
 images: [ImageRef], moodboards: [{id, name, link}], profiles: [{id, name, link}],
 codes: [{code, type, resolved}], parent_id, parent_status: 'none'|'in_export'|'missing'|'left_out',
 child_count, liked}
```

- `created_at` is normalized ISO UTC, or null. The original value stays in `raw`.
- **Deviation (additive):** `parent_status` is `left_out` when the parent is not in the export because the designer left it out (`meta.parent_gaps` marks it `left_out: true`); every other parent outside the export is `missing`.
- `kind_source` is `viewer` when the row had no usable `kind`. The viewer then applies the rules of `lib/normalize.js`, using the number of image slots in place of `batch_size`.
- In `codes`, `type` is the reference type, or `flag` for a `--profile` code that has no matching reference.
- `liked` is true when `image_likes` is non-empty or `likes.jsonl` lists the job.

`ImageRef`:

```
{index, status: 'present'|'missing'|'not_exported'|'unsupported', url, thumb_url,
 width, height, file, source_url, reason}
```

| Field | Meaning |
|---|---|
| `url` | `/files/<export_id>/<file_id>` when the status is `present`, else null. `thumb_url` is `url + '?size=thumb'`. |
| `index` | Taken from the file name `images/<job_id>_<i>.<ext>` when it matches and `i` is below the job's image count (the larger of its file and URL counts, and at least 4), else the first free position. A name such as `<job_id>_999.png` never adds empty slots. |
| `missing` | The path is safe and listed, but the file is not in the export. |
| `not_exported` | No file is recorded: images were off, video, a stopped run, or a board image outside the permitted hosts. |
| `unsupported` | The file exists or the path was refused, but it cannot be served. |
| `width`, `height` | API metadata: the job's dimensions, or the board image's. |
| `reason` | **Deviation (additive):** why the image is not `present`, as text. |

### `GET /api/exports/{id}/jobs/{job_id}`

`job_id` is one percent-encoded path segment. An unknown id gets `404 unknown_job`.

```
{job: JobCard + {full_command, flags, flags_source, profile_default, unknown_flags, job_type,
                 event_type, parent_index, parent_grid, video, image_likes},
 raw, unknown_fields, images,
 lineage: {ancestors: [JobCard], parent: {id, status, index},
           children: [JobCard + {parent_index}], descendant_count, cycle},
 references: [{type, id, code, user_id, source, link, target, reason}],
 moodboards: [{id, name, link, evidence}], profiles: [{id, name, link, evidence}],
 likes: [{image_likes, line}]}
```

- **Deviation (additive):** `flags_source` is `export`, `viewer` or `none`. `viewer` means `flags` and `prompt` were parsed from `full_command` with a port of `lib/parser.js`.
- `unknown_fields` lists unknown row keys, plus `flags.<key>` and `personalization_codes[].<key>`.
- `ancestors` are listed root first. `parent_index` comes from `parent_index`, else from an integer `parent_grid`.
- `children` are sorted by `parent_index`, then by date.
- `cycle` is true when the ancestor walk repeats a job or the job belongs to a cycle.
- `references` has one entry per raw reference, then one per extra `--profile` code. **Deviation (additive):** `source` is `reference` or `flag`.
  - `link` is `explicit`, `inferred_code`, `left_out` or `unresolved`. As in `lib/normalize.js`, a reference id links to every exported board and profile with that id, whatever the reference `type`; `explicit` never depends on the type.
  - **Deviation (additive):** `left_out` means no exported board or profile has the id, but `meta.selection.left_out_moodboard_ids` lists it: the designer left that moodboard out. A code match is then not used, `target` is null, and the reference counts as resolved in `codes`, so it never becomes an unmatched code.
  - `target` is `{kind: 'moodboard'|'profile', id, name}`, or null. When an id matches both a board and a profile, `target` is the one the type names (a profile for type `profile`, else the board), `reason` mentions the other, and both links are made.
  - `reason` is always text, and inferred reasons start with `inferred:`.
- `likes` lists the rows of `likes.jsonl` for this job, with line numbers.

### `GET /api/exports/{id}/moodboards`

`{items: [{id, name, code, codes, version, created_at, created_at_raw, reference_count, references_present, generations: {exporter, derived, inferred_code}, cover: [ImageRef, up to 4 present references]}], notes: [str]}`

**Deviation (additive):** `created_at_raw` is the value exactly as recorded (a string or number, else null). The exporter writes board dates without a time zone (`YYYY-MM-DD HH:MM:SS.ffffff`); `created_at` reads them as UTC, while the UI shows the raw value as written and says the zone is not recorded.

**Deviation (additive):** `generated_left_out` is there only when the board row records it as a whole number of 0 or more (exports in which the designer chose what to export): how many generations made with the board were not exported because of that choice.

### `GET /api/exports/{id}/moodboards/{board_id}`

`{moodboard: {id, name, code, codes, version, created_at, created_at_raw}, raw, unknown_fields, references: [ImageRef], generations: [{job: JobCard, link: 'exporter'|'derived'|'inferred_code', evidence}], unresolved_generated_job_ids: [ids], notes: [str]}`

`references` are the board's own pictures. `generations` are jobs made with the board, newest first. The two lists never mix.

**Deviation (additive):** `moodboard.generated_left_out` is there as in the list. When it is above 0, `notes` starts with `<N> more generations made with this moodboard were left out by the designer.` (without "more" when none of the board's generations is in the export).

### `GET /api/exports/{id}/profiles`

`{items: [{id, name, type, version, service, codes, ratings_count, created_at, created_at_raw, raw, unknown_fields, jobs: [{job_id, link: 'explicit'|'inferred_code', evidence}]}], unmatched_codes: [{code, type, job_ids}], notes: [str]}`

`created_at` is normalized (epoch-ms to ISO), or null. A code seen with several reference types has them joined by `/` in `type`.

### `GET /api/exports/{id}/likes`

`{items: [{job_id, job: JobCard|null, image_likes, source: 'likes.jsonl'|'jobs.jsonl'}], note}`

**Deviation:** `job_id` and `source` are additive. `job` is null when a like names a job that is not in the export. When `likes.jsonl` is absent or unreadable, items come from the jobs' `image_likes`, and so do jobs with likes that `likes.jsonl` omits.

### `GET /files/{export_id}/{file_id}[?size=thumb|original]`

- `file_id` is 20 hex characters from the export's own file table, never a path. Unknown ids get `404 unknown_file`, missing files `404 missing_file`.
- `Content-Type` comes from magic bytes. Only JPEG, PNG, WebP, GIF and AVIF are served; anything else gets `415 unsupported_media`, SVG included.
- The original is streamed in 256 KiB chunks with `Content-Length` and `Cache-Control: private, max-age=86400`. If an entry fails its CRC or changes size while streaming, the connection is closed, and from then on the file is `unsupported` (`415`), listed under `files.unreadable` and counted as such in the overview, the checks and the summary.
- `size=thumb` behaves as follows:
  - **Rendering.** Pillow renders the longest side at 512 px: JPEG, or WebP/PNG when the image has alpha. It decodes in draft mode and applies the EXIF orientation.
  - **Cache.** Thumbnails are cached at `<cache>/thumbs/<export_id>/<xx>/<sha256(export, member, size, crc-or-mtime, 512)>`.
  - **Limits.** At most 4 thumbnails render at once, and each cache key renders once even when many requests arrive together. Images over 80 megapixels, and anything Pillow cannot decode (including `DecompressionBombError`), fall back to the original bytes. So do images already 512 px or smaller. A `.orig` marker avoids decoding them again.
  - **No Pillow.** When Pillow is missing, or `--no-thumbnails` is set, every `size=thumb` request returns the original bytes.

## Data handling and limits

| Limit | Value |
|---|---|
| ZIP entries | 500,000. The end-of-central-directory record is checked before `zipfile` loads the directory. |
| `export_meta.json` | 16 MiB. |
| Each `*.jsonl` file | 256 MiB. |
| Single image read whole (thumbnails) | 128 MiB. |
| Folder root discovery | 3 levels deep and 2,000 folders. |

- **Byte caps.** The caps apply to the declared size first, and to the actual decompressed bytes while streaming. A bomb is never read past cap + 1 bytes.
- **Refused ZIP members.** These member names are refused and reported: absolute paths, drive letters, backslashes, control characters, empty, `.` or `..` segments, and duplicates. Symlink and special entries, encrypted entries and unsupported compression methods are reported and never read.
- **Folder files.** Folder paths follow the same rules. Their real path must stay inside the root's real path, and they are opened with `O_NOFOLLOW|O_NONBLOCK`. Non-regular files such as FIFOs are refused.
- **Dates.** Accepted forms: ISO with an offset, `Z` or `UTC`; naive `YYYY-MM-DD[ HH:MM:SS[.f]]`, treated as UTC; epoch numbers, read as milliseconds when the value is at least 1e11 and as seconds otherwise; numeric strings; null. All are normalized to UTC.
- **JSON values.** `NaN`, `Infinity` and out-of-range floats make the line invalid, so every response stays valid JSON.
- **Rows.** A row that is not an object, or that has no id, is skipped and reported with its line number. On a duplicate id, the first row wins.
- **Writes.** Nothing is ever written inside an export. The cache defaults to `$XDG_CACHE_HOME/mj-export-viewer` (or `~/.cache/mj-export-viewer`). A folder export containing the cache directory is refused.

## Command line: check

`python3 -m mjviewer --check PATH [--json]` validates one ZIP or folder without serving it, in either mode. It reads the export exactly as the server does and writes nothing. It also reads every referenced image file to its end once, where a ZIP checks each member's CRC: a file that fails is reported as unreadable (the `files_unreadable` warning) and not counted as present.

| Exit status | Meaning |
|---|---|
| `0` | Every export root opened and no check is at the `error` level. |
| `1` | At least one error-level check, a root that could not be read, or a source that could not be opened at all. |
| `2` | Usage problem: no file or folder at `PATH`, `--json` without `--check`, or `--check` combined with other paths, `--library`, `--hosted` or `--open`. |

`--json` prints:

```
{ok, path_name, error,
 exports: [{root, status, extension_version, version_support, export_time,
            counts: {jobs, moodboards, profiles, likes, images_expected, images_present, images_missing},
            selection,
            errors: [str], warnings: [str]}]}
```

- `path_name` is the last component of `PATH` only, never the full path.
- `root`, `status`, `extension_version`, `version_support` and `export_time` are as in `ExportSummary`; `counts` are the observed counts of the overview.
- **Deviation (additive):** `selection` is null when the designer did not choose what to export (or the record cannot be read, which is a warning), else `{chosen_at, moodboards: {available, exported, left_out}, jobs: {available, exported, left_out, not_reviewed}, text}` as in the overview, with the `selection` check as `"<title>: <detail>"` in `text`. It names no moodboard. What the designer left out is never an error or a warning.
- `errors` and `warnings` are the overview checks at that level, each as `"<title>: <detail>"` (or the title alone).
- **Deviation (additive):** `error` is `null`, or `{code, message}` when the source cannot be opened at all (`unreadable`, `not_a_zip`, `bad_zip`, `too_many_entries`, `no_export_root`, ...); `exports` is then empty. A root that fails to load is listed with `errors: ["could not read this export root (<ExceptionType>)"]`, and the other roots are checked as usual.

Without `--json` it prints a short summary per root (with the `selection` sentence, when there is one), then `error:` and `warning:` lines.

## Hosted mode

`python3 -m mjviewer --hosted` (module `mjviewer/hosted.py`, catalog and cache in `mjviewer/catalog.py`) serves the same UI and the same export routes behind a Google sign-in, with exports taken from a published catalog. Every setting comes from the environment; the README lists them. Unless stated below, routes and fields behave as in local mode.

### Hosted security

| Rule | Behaviour |
|---|---|
| Configuration | The server refuses to start (exit 2, one line per problem, no secret values) when `MJV_PUBLIC_URL`, `MJV_AUTH_SECRET`, `MJV_ALLOWED_DOMAIN`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` or `MJV_CATALOG_URL` is missing or weak. `MJV_PUBLIC_URL` must be an https origin (http only for `localhost` and `127.0.0.1`). `MJV_AUTH_SECRET` needs at least 32 characters and 10 different ones. `MJV_CATALOG_URL` must be https (http only for loopback), carry a query string, and its `sp` parameter, when present, may allow only `r` and `l`. The `MJV_GOOGLE_*_URL` test overrides must point at `localhost` or `127.0.0.1`. |
| Bind address | `0.0.0.0`, port `PORT` (default 8000). |
| Host header | Must be `MJV_PUBLIC_URL`'s host (`host` or `host:port`; the default port may be omitted or given); otherwise `403 forbidden_host`. `/healthz` and `/robots933456.txt` skip this check. |
| Session cookie | `__Host-mjv_session=<payload>.<mac>; Max-Age=<MJV_SESSION_DAYS × 86400>; Path=/; Secure; HttpOnly; SameSite=Lax`. The payload is base64url JSON `{email, iat, exp}`; the MAC is base64url HMAC-SHA256 under `MJV_AUTH_SECRET`, with a purpose prefix so that a sign-in cookie can never pass as a session. A session is valid when the MAC matches (constant-time), `exp` is in the future, `iat` is at most `MJV_SESSION_DAYS` old and not in the future, and the email is an address of `MJV_ALLOWED_DOMAIN`. |
| Without a session | `GET /`, `/index.html` and client routes (no dot in the last segment): **(deviation)** a browser (`Accept` names `text/html`) gets `200`, a small "Signing in" page whose script (`/static/signin.js`) sends it to `/auth/login?next=<path, query and #fragment>`, so that a shared link to a view comes back to that view (the server never sees the `#` part, and Google's account chooser would drop it after a redirect); the page also links to `/auth/login?next=<path and query>` for a browser without JavaScript. Other clients get `302 /auth/login?next=<path and query>`. `/api/*`, `/files/*` and everything else → `401 unauthenticated` (JSON). A request with more than one session cookie → `401 duplicate_session`, for pages too (a redirect could only loop). |
| No session needed | `/auth/login`, `/auth/callback`, `/auth/logout`, `/static/*` (no data; `signin.js` among them), `GET /favicon.ico` (`204`), `/healthz`, `/robots933456.txt`. |
| POST | Needs `X-MJV-Token` equal to `HMAC(session cookie)` (written into `index.html` as `{{MJV_TOKEN}}`), else `403 forbidden`, and an `Origin` header equal to `MJV_PUBLIC_URL`, else `403 forbidden_origin`. A missing `Origin` is refused. `Host` and `X-Forwarded-*` headers are never used for this comparison. **Deviation:** `POST /auth/logout` needs neither; it only clears cookies, like `GET /auth/logout`. |
| Headers | Everything local mode sends (the CSP is unchanged), plus `Strict-Transport-Security: max-age=31536000; includeSubDomains`. Every hosted answer is `Cache-Control: no-store` except static files and images, which keep the local values. |
| Secrets | The storage query string, `GOOGLE_CLIENT_SECRET` and `MJV_AUTH_SECRET` are kept in values that print as `<secret>`. They never appear in a log line, a page, an API answer, an error message or an exception text; network errors are described without their URL, and redirects are never followed. |
| Logging | As in local mode, plus `auth: signed in <email>`, refused sign-ins with the reason, catalog problems (positions and reasons, never entry content) and download results. |

### Probes

| Route | Response |
|---|---|
| `GET /healthz` | `200 {ok: true, mode: "hosted", commit, catalog: {exports, ok, age_s}, cache: {ok}}`. `commit` is `MJV_COMMIT` (letters, digits, `.`, `_`, `-`; otherwise `unknown`). `catalog.exports` is the number of usable entries, `ok` whether the latest fetch succeeded, `age_s` the age in seconds of the catalog in use (null before the first good fetch). **Deviation (additive):** `cache.ok` says whether the cache folders (`zips/`, `thumbs/`) accept writes, checked at start and every minute; while it is false no export can be opened, but the answer stays `200` so the platform does not restart instances in a loop (a deploy check should fail on it). No session, no Host check; it never fetches anything itself (the instance re-reads the catalog every minute on its own). |
| `GET /robots933456.txt` | `200 text/plain` `User-agent: *` / `Disallow: /`. The start probe some platforms send to a container. |

Other methods on both get `405`.

### Sign-in

| Route | Response |
|---|---|
| `GET /auth/login[?next=]` | `302` to Google's authorization endpoint with `client_id`, `redirect_uri=<MJV_PUBLIC_URL>/auth/callback`, `response_type=code`, `scope=openid email profile`, `hd=<MJV_ALLOWED_DOMAIN>`, `prompt=select_account`, `state=<random nonce>`, and **(deviation, additive)** PKCE `code_challenge=<S256 of a random verifier>` with `code_challenge_method=S256`. Sets `__Host-mjv_oauth` (`Max-Age=600; Path=/; Secure; HttpOnly; SameSite=Lax`): `{state, verifier, next, exp}` signed like the session. `next` is kept only when it is a same-origin path: it starts with `/`, the second character is not `/` or `\`, it holds only printable ASCII without spaces or backslashes, and it is at most 1,024 characters; otherwise `/`. |
| `GET /auth/callback?code&state` | The `state` must equal the nonce in a valid, unexpired `__Host-mjv_oauth` cookie (exactly one such cookie; constant-time comparison). The code is exchanged at the token endpoint (`grant_type=authorization_code`, client id and secret, `redirect_uri`, `code_verifier`), and the id token is checked at the tokeninfo endpoint: `aud` equals the client id, `iss` is `accounts.google.com` or `https://accounts.google.com`, `email_verified` is true (exactly `true` or `"true"`; never `1`), `hd` equals the domain, the email ends with `@<domain>` (exactly one `@`, compared in lower case), and `exp` is in the future (an integer, or a string of 1 to 12 ASCII digits). Success: `302` to `next`, setting `__Host-mjv_session` and clearing `__Host-mjv_oauth`. |
| Callback failures | A short HTML page with a "Sign in again" link (to `/auth/login`), clearing `__Host-mjv_oauth`: `401` "Sign-in cancelled" when Google returns `error`; `400` "Sign-in expired" for a missing, expired or mismatched state; `400` "Sign-in failed" for a missing code or one longer than 4,096 characters; `502` "Sign-in failed" when the token or tokeninfo endpoint fails, cannot be reached, redirects, or sends no usable id token or claims; `403` "Access refused" with the reason when a claim is wrong. Google's error bodies are never shown (the log keeps the HTTP status and an `error` code such as `invalid_grant`). |
| `GET` or `POST /auth/logout` | `200` "Signed out" page with a "Sign in" link; clears `__Host-mjv_session` and `__Host-mjv_oauth` (`Max-Age=0` with the same attributes, as the `__Host-` prefix requires). Sessions are not stored on the server: a copied cookie stays valid until it expires or `MJV_AUTH_SECRET` changes. |

### Pages

`GET /` (and client routes) returns `static/index.html` with `{{MJV_TOKEN}}` replaced by the session's request token and `{{MJV_MODE}}` by `hosted`; no cookie is set. **Deviation (additive, both modes):** `index.html` gained `<meta name="mjv-mode" content="{{MJV_MODE}}">`; local mode fills it with `local`. The UI uses it to show the published exports instead of upload and open-by-path, and the signed-in email with a "Sign out" link.

### Endpoints

#### `GET /api/health`

Local fields plus `mode: "hosted"` and `user: <signed-in email>`; `max_upload_bytes` is `0`.

#### `GET /api/exports`

`{exports: [ExportSummary], library: [], failed: [], catalog: {ok, updated_at, age_s, error, skipped: [{index, reason}], items: [CatalogItem]}}`

- `exports` lists the exports of ready items (open in the server instance that answered).
- `catalog.ok` is false while the latest fetch failed; `error` then says why (for example `catalog.json: storage refused the request (HTTP 403); the read-only access may have expired`) and the last good catalog is still served. `skipped` names unusable entries by position and reason only.
- Asking for this list refreshes the catalog when it is older than `MJV_CATALOG_TTL_S` (a conditional request with `If-None-Match`; one request at a time).

`CatalogItem`:

| Field | Meaning |
|---|---|
| `id` | The catalog id: the first 16 hex characters of the ZIP's SHA-256. |
| `name`, `label` | The original file name, and the display label or null. |
| `size` | Bytes of the ZIP. |
| `published_at`, `published_by` | Normalized ISO 8601 UTC string or null; text or null. |
| `summary` | `{jobs, moodboards, profiles, images, export_time, extension_version, status}` from the catalog, as published (counts or null, texts or null; `export_time` normalized). |
| `state` | `absent` (not in the cache), `cached` (in the shared cache, not opened by this instance), `queued`, `downloading` (here, or by another instance while this one waits for it), `verifying` (hashing a cached copy once), `opening` (building the index), `ready`, or `failed`. |
| `progress` | `{done, total}` in bytes while `downloading` or `verifying`, else null. While another instance downloads, `done` follows its temporary file. |
| `error` | `{code, message}` when `failed`, else null. Codes: `forbidden`, `not_found`, `storage_error`, `redirected`, `unreachable` (storage), `size_mismatch`, `truncated`, `download_failed`, `sha_mismatch`, `too_large_for_cache`, `cache_full`, `cache_unwritable`, `cache_unreadable`, `cancelled`, `internal`, or an open error of the local table (`not_a_zip`, `bad_zip`, `no_export_root`, ...). Messages name the published file, never a cache path. |
| `exports` | `[ExportSummary]` once `ready`, else `[]`. |
| `failed_roots` | **Deviation (additive):** once `ready`, the export roots of the ZIP that could not be read while the others opened, as `[{path, root, code, message}]` (`path` is the published file name); else `[]`. |

#### `GET /api/catalog/{id}`

One `CatalogItem`, or `404 unknown_catalog_entry`. The UI polls it while an export is prepared.

#### `POST /api/catalog/{id}/open`

Starts making the entry ready in the background and answers at once: `202` with the `CatalogItem` while the work runs, `200` when it is `ready` or `failed`, or `404 unknown_catalog_entry`. Idempotent: concurrent calls share one preparation. **Deviation:** a `failed` entry starts again only when the body is `{"retry": true}` (the UI sends it for a click on Open, Try again or Retry); any other body, or none, answers the failure as it is. Polls that reach another instance therefore never restart a broken download. A body that is not a JSON object gets `400 bad_request`, one over 4 KiB `413 too_large`. Preparation: when the ZIP is in the cache, it is verified (once per process, and again if another instance replaced the file); when it is not, this instance takes the download lock and downloads it, or, while another instance holds the lock, waits for that instance's file. No request ever waits for a download.

#### Export routes in hosted mode

`/api/exports/{id}/...` and `/files/{id}/{file_id}` are the local routes, with these differences:

- **Deviation:** hosted export ids are the catalog id (16 hex characters), or `<catalog id>-<n>` for the n-th root (in path order, from 1) when the ZIP holds several roots. They are the same in every instance and after restarts, so links and thumbnails can be shared.
- An id whose catalog entry is not open in the answering instance: when its ZIP is in the cache and no download of it is running, the instance verifies and opens it, waiting up to 20 seconds, then answers normally. Otherwise **(deviation, additive)** `409 {error: {code: "export_not_ready", message}, catalog: CatalogItem}` at once (during a download it does not wait); a GET never starts a download. The UI then opens it (`POST .../open`), shows the progress and repeats the request.
- An id that the catalog no longer lists is `404 unknown_export`, even when it is open.
- `ExportSummary.source` is `{type: "catalog", name: <published file name>, path: null}`; `label` is the catalog label (with ` › <root>` per root when there are several), else as in local mode.
- `POST /api/exports/open` and `POST /api/exports/upload` (any method) are `404 not_available`; `library` and `failed` are always empty.

### Cache and catalog rules

| Rule | Behaviour |
|---|---|
| Cache folder | `MJV_CACHE_DIR`; without it `/home/mjv-cache` when that folder exists and accepts writes, or when `/home` accepts writes to create it (**deviation, additive:** an existing writable `/home/mjv-cache` is used even under a read-only `/home`), else `$XDG_CACHE_HOME/mj-export-viewer` (`~/.cache/mj-export-viewer`), which the log names as not shared. |
| Catalog | `catalog.json` at the container root, at most 16 MiB and 10,000 entries: `{version: 1, updated_at, exports: [...]}`. Anything else (invalid JSON, `NaN`, another version, no `exports` list) keeps the last good catalog. |
| Entries | `sha256` is 64 lowercase hex characters, `id` its first 16, `blob` exactly `exports/<sha256>.zip`, `size` an integer from 22 bytes to 256 GiB, `name` a plain file name (at most 255 characters, no `/`, `\` or control characters), `label` and `published_by` short texts or null, `published_at` a date or null, `summary` an object of counts and short texts. A failing entry is skipped and reported; its blob is never requested. A duplicate id keeps the first entry. |
| Object URLs | `<container URL>/<object name>?<query string>`. GET only, no redirects followed. `catalog.json` gets a 10-second timeout, a ZIP a 30-second socket timeout. |
| Download | `<cache>/zips/.dl-<sha256>-<owner>-<random>.part` in the same folder, streamed and hashed; `Content-Length` must equal the catalog size, the byte count may not exceed it and must reach it, and the SHA-256 must match. Then `fsync`, mode `0444`, atomic rename to `<cache>/zips/<sha256>.zip`. On any failure the temporary file is removed. At most two downloads or verifications run at once per instance. |
| Several instances | `.dl-<sha256>.lock` (created exclusively, touched every 5 s while downloading) marks the instance that downloads a ZIP; a lock untouched for 60 s is broken. Temporary files untouched for 10 minutes and stale locks are removed at start, every minute and before each download. On shutdown (`SIGTERM`) an instance stops its downloads and waits up to 5 seconds for them to remove their temporary files and locks, so the others take over at once (a download stalled inside a network read is left to the rules above). **Deviation:** the lock file is the one write that is not a temporary file plus a rename, since exclusive creation is the coordination itself. |
| Links | A symbolic link (or anything but a regular file) in `<cache>/zips` is never followed: it counts as absent, is replaced by a download and is removed, not its target. |
| Eviction | Before a download, the least recently used ZIPs (by modification time; a ZIP is touched when opened and at most every 5 minutes while used) are deleted until the new one, plus the downloads under way in any instance, fits under `MJV_CACHE_MAX_GB`. A ZIP that this instance is preparing, or that any instance used in the last 10 minutes, is never deleted; an evicted ZIP that was open here is closed here. **Deviation (stricter):** when that leaves too little room, the download fails with `cache_full` instead of going past the limit. A ZIP larger than the whole limit fails with `too_large_for_cache`. |
| Unpublished exports | Every minute each instance re-reads the catalog (conditionally) and, on the latest good one, closes the exports it no longer lists, deletes their thumbnail folders, and deletes their cached ZIPs once no instance has used them for 10 minutes. This happens while nobody uses the viewer too, and never before a catalog has been read once. |
| Thumbnails | `<cache>/thumbs/<export id>/...`, as in local mode; with hosted ids they are shared by every instance. |
