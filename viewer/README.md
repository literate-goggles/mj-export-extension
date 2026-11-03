# MJ Export Viewer

A local, read-only viewer for the exports that the MJ Export extension in this
repository writes (`mj_export_<user>_<date>[_<ms>]/` folders, or a ZIP of one).
It runs a small Python web server on your own machine and shows the export in
your browser: an overview with completeness checks, a searchable gallery, job
pages with lineage, moodboards, profiles and likes.

The viewer is separate from the extension. It is not loaded by Chrome, it is not
in the designers' distribution ZIP (`scripts/package.py` packages an explicit
allowlist that does not include `viewer/`), and it never contacts Midjourney.
Run locally, it contacts no other site either; the optional
[hosted mode](#hosted-mode) for a team talks only to Google sign-in and to the
deployment's own private storage. Exports are opened in place and never
modified.

What an export contains, file by file, is described in
[EXPORT_OVERVIEW.md](EXPORT_OVERVIEW.md).

## Requirements

- Python 3.9 or newer, standard library only. No `pip install`, npm or build step.
- Optional: [Pillow](https://pypi.org/project/Pillow/) for 512 px thumbnails
  (`python3 -m pip install --user Pillow`). Without it the gallery shows the
  original images, which works but is heavier on large exports.
- A current browser. The UI is tested in Chromium; it uses no external scripts,
  fonts or CDNs, so it works offline.

## Run it

From the repository root:

```sh
cd viewer
python3 -m mjviewer ~/Downloads/mj_export_<user>_<date>.zip
```

Then open the address it prints, `http://localhost:8765/`. With exactly one
export open, the viewer goes straight to its overview.

- **Several exports:** pass several paths (ZIPs or folders):
  `python3 -m mjviewer a.zip b.zip ~/Downloads/mj_export_<user>_<date>/`.
  A ZIP that holds several export roots opens each of them.
- **A folder of exports:** `--library ~/Downloads/mj-exports` lists that
  folder's `*.zip` files and export folders on the start page; open one with a
  click. Repeat `--library` for more folders.
- **From the browser:** the start page can upload a ZIP (it is copied into the
  cache; the original is untouched) or open a ZIP or folder by its path on the
  machine that runs the viewer.
- **On a remote machine** (for example a shared workstation): start the viewer
  there as usual, then forward the port from your laptop and open the local
  address:

  ```sh
  ssh -L 8765:127.0.0.1:8765 <host>
  # then open http://localhost:8765 on the laptop
  ```

  Keep the default `--host 127.0.0.1`. Binding to another address is possible,
  but requests must still name `localhost` (or a name given with `--allow-host`),
  and anyone who can reach the port could try to use it.

Options:

| Option | Default | Meaning |
|---|---|---|
| `PATH ...` | none | Export ZIPs or folders to open at start. |
| `--library DIR` | none | Offer the ZIPs and export folders in `DIR` on the start page (repeatable). |
| `--port N` | 8765 | Port to listen on (`0` picks a free one). |
| `--host ADDR` | 127.0.0.1 | Address to bind. |
| `--cache-dir DIR` | `$XDG_CACHE_HOME/mj-export-viewer` or `~/.cache/mj-export-viewer` | Thumbnails and uploaded ZIPs. Never inside an export. |
| `--no-thumbnails` | off | Always serve the original image bytes. |
| `--max-upload-mb N` | 4096 | Largest ZIP the upload accepts. Opening by path has no such limit. |
| `--allow-host NAME` | none | Extra `Host` name to accept besides localhost (repeatable). |
| `--open` | off | Open the browser at start. |
| `--check PATH` | none | Validate one export ZIP or folder and exit, without serving it (see below). |
| `--json` | off | With `--check`: print the report as JSON. |
| `--hosted` | off | Run the hosted mode for a team; every setting comes from the environment (see [Hosted mode](#hosted-mode)). |

Stop the server with Ctrl+C.

To check an export without opening the viewer, for example before sharing or
publishing it:

```sh
python3 -m mjviewer --check ~/Downloads/mj_export_<user>_<date>.zip          # short summary
python3 -m mjviewer --check ~/Downloads/mj_export_<user>_<date>.zip --json   # machine-readable
```

It reads the export exactly as the viewer does and prints, for each export
root, its status, exporter version, export time, counts and the error- and
warning-level checks of the overview. The exit status is 0 when every root opened
without errors, 1 when there are errors (or nothing could be read), and 2 for a
usage problem such as a path that does not exist. The JSON fields are in
[API.md](API.md#command-line-check).

## What each view shows

- **Exports** (start page): every open export with its status, exporter
  version, export time and counts, plus upload and open-by-path. Paths given on
  the command line that could not be opened are listed with the reason.
- **Overview:** status, exporter version, account, the scope that was exported
  (all jobs, the first N, or jobs since a date; images on or off; small test),
  whether the job feed was read to the end, and the export time (when
  `export_meta.json` was last written: the end or stop of the latest run).
  Completeness checks are grouped by severity, the counts in `export_meta.json`
  sit next to what is really in the files, and missing, unreferenced and
  unreadable files, the exporter's failures, parent gaps and unresolved links
  are listed. When the designer chose what to export, the overview says so in
  plain words and shows how many moodboards and generations were exported and
  left out.
- **Gallery:** every job as a card, newest first, with lazily loaded thumbnails
  (only the images in view load; more load as you scroll) and pages of 60 jobs
  added as you reach the end. A request the browser drops because the
  computer's network changed is retried once. Search the prompts (every word must match,
  case-insensitive) and filter by job kind, model version, moodboard, profile or
  unmatched code, and creation date. The filters live in the address, so a
  filtered view can be bookmarked; Back from a job returns to the same list and
  scroll position.
- **Job:** all images (click for a full-size viewer), the full command, the
  parsed flags (unknown flags are kept as written), dimensions, kind with its
  `job_type` and `event_type`, version and where it came from, file paths and
  source URLs (shown as text, never loaded), the personalization references and
  what each resolves to (a moodboard the designer left out of the export says
  so), the raw `jobs.jsonl` row, and the lineage: the chain of parents up to the
  root, the image of the parent it was made from, and the children made from
  it, all as links. A parent the designer left out is shown as left out.
- **Moodboards:** each board with its codes and counts. A board page keeps its
  **reference images** (the pictures saved on the board, in amber frames) apart
  from the **generations** (your jobs made using the board, as job cards), and
  says how each generation is linked, and how many more generations made with
  the board are not in the export because of the designer's choice (left out,
  or made after choosing).
- **Profiles:** custom profiles with their codes, aggregate rating counts and
  linked jobs; the global profiles the exporter synthesizes from counts; and
  personalization codes that match nothing in the export, each with its jobs.
- **Likes:** the `image_likes` of your exported jobs as recorded, or an explicit
  empty state.

Keyboard: `/` focuses the search and Esc clears it; on a job opened from the
gallery, ← and → move to the previous and next job; the image viewer takes ←,
→, Home, End and Esc. The theme button cycles System, Light and Dark; the
choice is kept in the browser's `localStorage` under `mjv-theme`.

## How links are labelled

The viewer never invents a relationship. Every link says where it comes from:

| Label | Meaning |
|---|---|
| Exporter link | The board's `generated_job_ids` (written by the exporter) lists the job. |
| Derived from job reference | The job's personalization reference has the board's id, but the board row has no `generated_job_ids` (an older export), or that list leaves the job out. The second case is also reported as a warning, because the exporter builds the list from exactly these references. |
| Explicit reference | A job's personalization reference has the profile's id. |
| Inferred by code match | Only a code matches: a reference code or a `--profile` flag code is one of the board's or profile's codes. Likely, but not proven, so it is always marked and the "Explicit links only" switch hides it. |
| Left out by the designer | The reference's id is a moodboard the designer chose not to export (`export_meta.json` lists it in `selection`), so there is nothing in the export to link to. It is not counted as unresolved. |
| Unresolved | The reference matches nothing in the export, for example a `user` reference that has a code but no id. It stays visible as an unmatched code you can filter by. |

Lineage uses `parent_id` only, never the feed's selection flags. The image a
job was made from comes from `parent_index`, else from an integer
`parent_grid`. A parent outside the export is shown as a gap (cross-checked
with `export_meta.json`'s `parent_gaps`), unless `parent_gaps` says the
designer left it out; then it is shown as left out. Walks stop if parent links
ever form a cycle. Ids are compared as exact strings.

Other things the viewer shows as recorded rather than guessing:

- Job kinds come from the exporter's keyword rule on `job_type` and
  `event_type`; `grid` and `other` are its heuristics. When a row has no kind,
  the viewer applies the same rule and says so.
- The model version comes from `--niji`, else `--v` (`7.0` and `7` are one
  version), else a `job_type` such as `v8-2_diffusion`; otherwise it is
  Unknown.
- Moodboard creation times are written without a time zone; they are shown as
  written, with a note.
- Global profiles are synthesized by the exporter from aggregate counts; rating
  counts are counts, not ratings.
- `image_likes` has no documented shape, so its values are shown as recorded.
- Reference images are the board's pictures at export time, which may differ
  from the set a generation was made with.

## Compatibility and odd data

Exports from extension 0.3.0 and 0.3.1 (one writer) and 0.4.0 (the same files
plus the designer's selection) are marked supported. Anything else, older
versions included, is read with best effort and a warning.
The viewer copes with:

- a ZIP with or without a top folder, several export roots in one ZIP, and
  macOS `__MACOSX/`, `._*` and `.DS_Store` entries;
- missing or empty files (an empty `likes.jsonl` simply means no likes), lines
  that are not valid JSON (skipped and listed with file and line number), and
  unknown fields (kept in the raw record and listed);
- dates as ISO strings with an offset, strings without a zone, epoch
  milliseconds or nothing;
- partial runs (`paused`, `stopped`, failures), metadata-only exports, images
  that are missing, unreferenced, unreadable or not a supported raster type,
  and a root without `export_meta.json` (reported as an interrupted run while
  everything else is still shown);
- exports in which the designer chose what to export (`selection` in
  `export_meta.json`): what they left out is labelled as left out, never as
  missing or unresolved; selection values the viewer cannot read are ignored
  with a warning, and values that contradict each other or the files are
  shown as recorded with a warning;
- an export that cannot be read at all: the server reports it and keeps
  running.

## Security model

The viewer handles personal data and, run locally, is built so that it can
only be used from your own browser session (the hosted mode has
[its own model](#security-model-in-hosted-mode)):

- **Local only.** It binds to `127.0.0.1` by default and rejects requests whose
  `Host` header is not `localhost`, `127.0.0.1` or `[::1]` (or an
  `--allow-host` name), which blocks DNS-rebinding pages.
- **Per-run secret.** Each start creates a random secret. The start page sets it
  as an `HttpOnly`, `SameSite=Strict` cookie, and every API and image request
  needs that cookie. Every request that is not a GET (open, upload) also needs
  the secret in an `X-MJV-Token` header, and is refused when the browser says it
  comes from another origin, so other sites cannot use the API through your
  browser.
- **Strict browser policy.** Every response carries a Content Security Policy
  that allows only the viewer's own scripts, styles, images and requests (no
  inline code, no other hosts), plus `nosniff`, `no-referrer` and same-origin
  resource headers. Prompts, names and other export text are rendered as text,
  never as HTML; source URLs are displayed, never loaded.
- **Read-only access.** ZIPs are read member by member and never extracted;
  folders are read through validated relative paths that must stay inside the
  export's real path (symbolic links out of it are refused). Nothing is written
  into an export, and a folder that contains the viewer's cache is refused.
- **Safe ZIP handling.** Entry names that are absolute, contain `..`,
  backslashes or control characters, symlink and special entries, encrypted
  entries and unknown compression methods are refused and listed. The archive
  directory is checked before it is loaded (at most 500,000 entries), and
  metadata files are capped by their real decompressed size
  (`export_meta.json` 16 MiB, each JSONL file 256 MiB), so a ZIP bomb is never
  read past its cap.
- **Images by id.** The browser asks for images by an opaque id from the
  server's own file table, never by path. Files are served only when their first
  bytes are JPEG, PNG, WebP, GIF or AVIF (SVG and anything else are refused),
  and thumbnails are rendered with Pillow's decompression-bomb limit.
- **Quiet logs.** Only failed and non-GET requests are logged, without query
  strings, so search terms never reach the log.

## Privacy

An export is personal data: prompts, images, account ids, moodboards and
profile codes.

- Keep exports out of Git. The repository ignores `mj_export_*/` folders at its
  root and everything under `test-exports/`, and `viewer/.gitignore` ignores
  exports and ZIPs placed in this folder. Do not commit an export, a screenshot
  of one, or a test fixture made from one; the tests here use invented data only.
- The cache directory (default `~/.cache/mj-export-viewer`, see `--cache-dir`)
  holds thumbnails of the images you viewed (`thumbs/`) and a copy of every ZIP
  uploaded through the browser (`uploads/`). Nothing there is ever pruned.
  To clear it, stop the viewer and delete the folder:

  ```sh
  rm -rf ~/.cache/mj-export-viewer        # or your --cache-dir
  ```

- The browser keeps only the theme choice and the running viewer's session
  cookie. Viewing happens on your machine; nothing is sent anywhere.

## Hosted mode

The hosted mode serves exports to colleagues from one deployment: they sign in
with Google and see the exports that were published to it. It is the same
viewer with the same views and the same read-only ZIP reader; what changes is
who can reach it and where exports come from. Local mode is unaffected.

```sh
python3 -m mjviewer --hosted   # every setting comes from the environment
```

- **Sign-in.** Any account of one Google Workspace domain can sign in. The
  server exchanges Google's code itself and checks the id token with Google's
  tokeninfo endpoint: it must be for this app, from Google, unexpired, with a
  verified email, and both its `hd` claim and the email address must name your
  Workspace domain. The session is a signed cookie, valid for 30 days by default.
- **Exports are published, never uploaded.** A deployment keeps its exports in a
  private blob container (for example in Azure Blob Storage) holding
  `catalog.json` and `exports/<sha256>.zip`, and gives the server a read-only
  query string for it (for example a container SAS that allows read and list
  only). Publishing happens outside the viewer, from a machine that has the
  export: check it with `--check`, upload the ZIP, then add its entry to
  `catalog.json`. The hosted mode has no upload, no opening by path and no
  library, and shows no file path.
- **Owners and deleting.** An export's catalog entry can name its owner: the
  email address of the designer it belongs to, recorded when it is published.
  Everyone who signs in sees every published export with its owner (or "No owner
  recorded"). When deleting is turned on (`MJV_CATALOG_WRITE_URL`), the owner,
  and the administrators in `MJV_ADMIN_EMAILS`, see **Delete…** on its card; a
  dialog names the export and asks to confirm. It stays open until the request
  ends; when deleting fails it says so in plain words (whether anything was
  deleted, and what to do), and the server log has the details. An export
  without an owner can be deleted by administrators only.
- **What deleting does.** The export leaves `catalog.json` (a short record of
  the deletion, with no name or label, stays in its `deleted` list), its ZIP is
  deleted from the container, and the instance that deleted it deletes its
  cached copy and thumbnails at once. Other instances do the same when they
  next read `catalog.json` successfully, within about a minute. An instance
  that cannot read it keeps its last good copy for at most 5 minutes (or 5 ×
  `MJV_CATALOG_TTL_S`, when that is longer); then it closes its exports and
  says "The viewer can't reach its list of exports right now" until a read
  succeeds. Images a browser has already shown can stay in its cache for up to
  10 minutes. It cannot be undone in the viewer. Whether the container keeps a
  recoverable copy of a deleted file, and for how long, is its own setting
  (soft delete in Azure Blob Storage, for example): set `MJV_SOFT_DELETE_DAYS`
  to that number of days and the dialog says the storage keeps a recoverable
  copy that long, then deletes it; without it the dialog says that the
  storage's own settings decide. The dialog also says that copies shared in
  other ways (the ZIP that was sent to publish it, for example) are not
  affected.
- **Hiding generations and moodboards.** While deleting is turned on
  (`MJV_CATALOG_WRITE_URL`), an export's owner and the administrators in
  `MJV_ADMIN_EMAILS` see **Hide…** on the page of each generation and
  moodboard. Hiding a generation hides that one generation. Hiding a moodboard
  also hides its reference images and every generation its page lists,
  whatever the link (from `generated_job_ids`, derived from a reference, or
  inferred from a code), in every export root of the ZIP, plus the generations
  in other roots of the ZIP that reference it by its id. Before anything is
  hidden, a dialog says what stays: how many generations made from it
  (upscales, variations and the like) stay visible, and for a moodboard how
  many generations it hides. Everyone else then sees the export as if those
  items had never been exported: they are gone from the gallery, search,
  filters, counts, checks, the overview and the moodboard, profile and likes
  pages; their links and image addresses answer Not found, exactly like ids
  that never existed; a generation made from a hidden one shows "Parent not
  available"; a moodboard reference picture that is a hidden generation's
  picture shows "Not available". The owner and the administrators browse the
  same views, plus a **Hidden** section on the export's overview that lists
  what is hidden (who hid it and when; a moodboard with how many generations
  went with it), the hidden items' own pages, reached from there or by their
  links, with a "Hidden from everyone else" banner, and **Show again…**. The
  list belongs to the export's entry in `catalog.json` (see
  [Catalog](#catalog)), so it holds on every instance: the instance that
  changed it applies it before it answers, the others when they next read
  `catalog.json` (within about a minute, `MJV_CATALOG_TTL_S`); without sticky
  sessions a page reloaded right after a change may reach an instance that has
  not read it yet and show the old state until it has. Turning writes off keeps
  hidden items hidden from everyone, owners and administrators included, and
  nobody can show them again until writes are back. When an instance cannot
  apply a list (building the filtered index fails), everyone else sees "could
  not prepare this export" with Try again, while the owner and the
  administrators still see the Hidden section and can show items again.
- **What hiding leaves in place.** Hiding changes what this viewer shows, not
  the export. The stored ZIP still holds every hidden prompt and image, and so
  do other copies: the file that was published, downloads, backups and the
  storage's own recoverable copies. Generations made from a hidden one
  (upscales, variations, pans, zooms, remixes) stay visible, usually with the
  same prompt; an upscale shows one of the hidden grid's pictures, and a hidden
  upscale's picture stays visible in its grid. Prompts and flags of visible
  generations that use a hidden picture by its address (image prompts,
  `--sref`, `--cref`) keep that address, which names the hidden generation and
  opens its picture at Midjourney. In a ZIP with several export roots, a
  generation in another root that names a hidden moodboard only by its code
  keeps showing that code. Fields this viewer does not know are shown as
  written. Browsers may keep images they already showed for up to 10 minutes,
  and a page that is open keeps what it showed until it is reloaded. The list
  goes with the export's catalog entry: once the export is deleted (or
  unpublished), publishing the same ZIP again, like publishing a new one,
  starts with nothing hidden. To remove something for real, delete the export
  and publish one without it.
- **Versions.** A viewer older than this one ignores hidden lists and shows
  everything to everyone. `/healthz` lists `hide` in `features`: check it
  before rolling back, and while instances are upgraded.
- **Opening a published export** downloads its ZIP in the background into the
  cache, checks its size and SHA-256 against the catalog, makes it read-only,
  and then reads it like a local ZIP. The start page shows the progress; with a
  single published export the viewer goes straight to its overview once it is
  ready. A link to a view of an export that is not open yet opens it first, and
  a link opened before signing in comes back to that view after the sign-in.
  When opening fails, the instance keeps the error until someone clicks Try
  again, so it never downloads a broken export again on its own; with several
  instances, each one that has not tried yet may still try once.
- **Several instances** (behind a load balancer) can share the cache folder, for
  example a shared mount: one instance downloads a ZIP while the others wait for
  it, and each verifies a cached ZIP once before it uses it. No sticky sessions
  are needed. Without a shared folder every instance downloads on its own.

### Settings

| Variable | Default | Meaning |
|---|---|---|
| `MJV_PUBLIC_URL` | required | The https origin users open, such as `https://viewer.example.com`. Google returns to `<MJV_PUBLIC_URL>/auth/callback`, so register that address as a redirect URI of the OAuth client. Requests must name this host. `http://localhost:<port>` and `http://127.0.0.1:<port>` are accepted for local tests. |
| `MJV_AUTH_SECRET` | required | At least 32 random characters (for example `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`). It signs the session and sign-in cookies and the request tokens; changing it signs everybody out. |
| `MJV_ALLOWED_DOMAIN` | required | Your Workspace domain, such as `example.com`. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | required | A Google OAuth client of type "Web application". |
| `MJV_CATALOG_URL` | required | The container URL with its read-only query string, such as `https://<account>.blob.core.windows.net/<container>?sv=...&sp=rl&sig=...`. https only. A SAS whose `sp` allows more than read and list is refused. Treat it like a password. |
| `MJV_CATALOG_WRITE_URL` | none | Turns deleting and hiding on. The same container as `MJV_CATALOG_URL` (the same scheme, host and path) with a query string of its own that may also write and delete: a SAS whose `sp` contains `w` and `d` and nothing but `r`, `w`, `d` and `l`, such as `sp=rwdl`. https only. It is used only to rewrite `catalog.json` and to delete the ZIPs of deleted exports; everything is still read with `MJV_CATALOG_URL`. Treat it like a password. Without it the viewer never writes to the container and nobody can delete, hide or show again (lists hidden before still apply to everyone). |
| `MJV_SOFT_DELETE_DAYS` | none | How many days the container keeps a deleted file recoverable (its soft-delete setting), a whole number from 1 to 365. The delete dialog states it. The viewer cannot check it, so set it to what the container really keeps; without it the dialog says that the storage's own settings decide whether a recoverable copy is kept. |
| `MJV_ADMIN_EMAILS` | none | Administrators: email addresses, separated by commas and/or spaces, who may delete any export, also one without an owner, and hide its generations and moodboards or show them again. Each must be an address at `MJV_ALLOWED_DOMAIN`; they are compared in lower case. They can do so only while deleting is turned on. |
| `PORT` | 8000 | Port to listen on; hosted mode binds all interfaces. |
| `MJV_CACHE_DIR` | `/home/mjv-cache`, else `~/.cache/mj-export-viewer` | Downloaded ZIPs and thumbnails. The first default is used when that folder exists and accepts writes, or when `/home` accepts writes to create it (a shared, persistent mount on some platforms); otherwise the second (under `$XDG_CACHE_HOME` when that is set) is used and the log says so. Whichever folder is used is checked for writes at start and every minute; `/healthz` reports the result as `cache.ok`. |
| `MJV_CACHE_MAX_GB` | 50 | The cache never grows past this size (in GiB). Before a download, the least recently used ZIPs that no instance used in the last 10 minutes are deleted to make room; when that is not enough, opening fails with "the cache is full" until some of them have not been used for 10 minutes. |
| `MJV_SESSION_DAYS` | 30 | Session length. Lowering it also ends older sessions. |
| `MJV_CATALOG_TTL_S` | 60 | How long the catalog is reused before it is fetched again (with `If-None-Match`). |
| `MJV_COMMIT` | `unknown` | A build label that `/healthz` reports. |
| `MJV_GOOGLE_AUTH_URL`, `MJV_GOOGLE_TOKEN_URL`, `MJV_GOOGLE_TOKENINFO_URL` | Google's | For tests only: accepted only when they point at `localhost` or `127.0.0.1`. |

The server refuses to start, with exit status 2 and one line per problem, when
a required setting is missing or weak (a short or repetitive secret, an http
address, a storage query string that is not read-only), when
`MJV_CATALOG_WRITE_URL` names another container or does not allow write and
delete (or allows more than read, write, delete and list), when an
administrator's address is not at `MJV_ALLOWED_DOMAIN`, or when a number
(`PORT`, `MJV_SOFT_DELETE_DAYS` and the like) is not one or is out of its range.
It never prints a secret value.

### Catalog

`catalog.json` sits at the top of the container:

```json
{
  "version": 1,
  "updated_at": "2026-04-01T10:00:00Z",
  "exports": [
    {
      "id": "<first 16 hex characters of sha256>",
      "blob": "exports/<sha256>.zip",
      "name": "mj_export_<user>_<date>.zip",
      "label": "Optional display name",
      "sha256": "<64 lowercase hex characters>",
      "size": 123456789,
      "published_at": "2026-04-01T10:00:00Z",
      "published_by": "someone@example.com",
      "owner": "designer@example.com",
      "summary": {"jobs": 120, "moodboards": 4, "profiles": 2, "images": 480,
                  "export_time": "2026-03-31T18:00:00Z", "extension_version": "0.3.1", "status": "complete"},
      "hidden": {
        "jobs": [{"id": "<job id>", "hidden_at": "2026-04-02T08:00:00Z", "hidden_by": "designer@example.com"}],
        "moodboards": [{"id": "<moodboard id>", "hidden_at": "2026-04-02T08:05:00Z", "hidden_by": "admin@example.com"}],
        "counts": {"jobs": 13, "moodboards": 1, "images": 50}
      }
    }
  ],
  "deleted": [
    {"id": "<first 16 hex characters>", "sha256": "<64 hex characters>", "size": 98765,
     "owner": "designer@example.com", "deleted_at": "2026-04-02T09:00:00Z", "deleted_by": "designer@example.com"}
  ]
}
```

Each entry is checked strictly: `id` must be the first 16 characters of
`sha256`, `blob` must be exactly `exports/<sha256>.zip`, `size` must be a byte
count, `owner` (optional) must be an email address (kept in lower case), and
the texts must be short and plain. An entry that fails is skipped,
listed in the server log by position and reason, counted on the start page, and
never downloaded. When the catalog cannot be read, the last good copy stays in
use and the start page says so, for at most 5 minutes (or 5 ×
`MJV_CATALOG_TTL_S`, when that is longer) without a successful read: after
that the instance serves no export until it can read the catalog again. An
export removed from the catalog disappears within `MJV_CATALOG_TTL_S` seconds.
Every minute, also while nobody uses the viewer, each instance re-reads the
catalog, closes the exports it no longer lists and deletes their thumbnails;
their cached ZIPs are deleted once no instance has used them for 10 minutes.

`hidden` (optional) is what the app hides in that export: the ids of the jobs
and moodboards hidden from everyone but the owner and the administrators, each
with who hid it and when, and `counts`, which the app writes at every change
(generations and moodboards hidden in the ZIP, a moodboard's generations
included, and the image files only they use, summed over its export roots).
The cards subtract `counts` from `summary` before an export is opened. Which
ids an export hides must be read with certainty, so the whole entry is skipped
like any invalid one (never served without its list) when `hidden` is not an
object, `jobs` or `moodboards` is not a list (absent or `null` is empty), an
item has no usable `id` (a string of 1 to 200 characters, no control
characters, not only spaces) or the lists hold more than 1,000 items together;
such an entry also keeps its id, so a later entry with the same id is skipped
too. A `hidden_at` that is not a date, a `hidden_by` that is not an address or
`counts` that are not three whole numbers are only shown as not recorded, and
an id listed twice counts once. A job id that no export root has hides
nothing, but a moodboard id that no root has still hides the generations that
reference it by that id (a selection can leave a moodboard out and keep them);
both are listed as "Not in this export". Removing an item, or the whole
`hidden` key, by hand shows it again within a catalog read; drop `counts` when
editing items rather than leave wrong numbers (cards then show no counts until
the app writes the list again). The app never grows `catalog.json` past
12 MiB (the server reads up to 16 MiB): a hide that would is refused, while
showing again and deleting, which make it smaller, still work.

`deleted` holds one record per deleted export (no name, label or summary). An
instance that reads one closes that export and deletes its cached ZIP and
thumbnails at once, without the 10-minute wait, unless the export is listed
again (published anew). For 30 days, every minute, an instance that may delete
also deletes that export's ZIP again while the storage still has it (when the
first try failed), but never a ZIP uploaded again after the deletion. The
viewer rewrites `catalog.json` from its raw JSON, so fields it does not know
survive, and only on the ETag it has just read (`If-Match`); when another
writer got there first it reads the file again and starts over, a few times at
most. Whatever else writes `catalog.json` must keep fields it does not know and
write the same way.

### Health and start probe

- `GET /healthz` answers without sign-in and for any `Host`:
  `{"ok": true, "mode": "hosted", "commit": "...", "features": ["hide"], "catalog": {"exports": 2, "ok": true, "age_s": 12.5, "delete_enabled": false, "zip_deletes_pending": 0, "soft_delete_days": null}, "cache": {"ok": true}}`.
  It names no export, user, path or secret. `features` lists what this
  version does that older ones did not: `hide` means it applies hidden lists
  (an instance without it would show every hidden item to everyone).
  `catalog.ok` is false while the
  catalog cannot be read; `age_s` is the age of the copy in use;
  `delete_enabled` says whether `MJV_CATALOG_WRITE_URL` is set;
  `zip_deletes_pending` counts the ZIPs of deleted exports that the instance
  still has to delete from storage (it keeps trying every minute; alert when
  it stays above 0); `soft_delete_days` is `MJV_SOFT_DELETE_DAYS` (null when
  it is not set). `cache.ok` is
  false while the cache folder does not accept writes (nothing can be opened
  then); the answer stays `200`, so check it after a deploy.
- `GET /robots933456.txt` answers `200` with plain text. Some platforms (App
  Service among them) request this path to see that a container has started.

### Security model in hosted mode

What differs from [local mode](#security-model):

- **Sessions instead of a per-run secret.** After sign-in the server sets
  `__Host-mjv_session`: `{email, iat, exp}` signed with HMAC-SHA256,
  `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`. Every request checks its
  signature, its age and its domain. A request that carries two session cookies
  is refused.
- **Everything needs a session** except the sign-in routes, `/static/*`, the
  icon and the two probes. A page opened without one starts the sign-in from a
  small page of its own, so it comes back to the same view, `#` part included
  (a plain redirect would lose it); other clients get a redirect. API and image
  requests get `401`.
- **Host and Origin.** Requests must name `MJV_PUBLIC_URL`'s host. A POST also
  needs `X-MJV-Token`, an HMAC of the session written into the page, and an
  `Origin` equal to `MJV_PUBLIC_URL` (never compared with `X-Forwarded-*`
  headers).
- **Sign-in.** The `state` value is bound to a signed, 10-minute
  `__Host-mjv_oauth` cookie, the code is protected with PKCE, and the address
  to return to must be a path on the same site.
- **Downloads** follow no redirects, are checked for size and SHA-256 before
  they become visible, and are written as temporary files that are renamed
  atomically. A link placed in the cache folder is never followed. On
  shutdown a download in progress removes its temporary file and its lock.
- **Requests with a body.** Any request that declares one, a GET included,
  closes its connection after the answer, so a front end that shares
  connections between users can never mistake a body for the next request.
- **Deleting** is a POST like the others (token and `Origin` checked) that must
  repeat the export's id in its body. Whether the signed-in user may delete is
  decided on `catalog.json` as read from the storage at that moment: the owner
  recorded there, or an administrator. Nothing is ever written to the
  container without `MJV_CATALOG_WRITE_URL`, and reading never uses it.
- **Hiding** is a POST of one generation or moodboard, checked the same way and
  written the same way. Every export route and every image goes through one
  check that serves everyone but the export's owner and administrators an
  index built without the hidden items, from the start, so hidden ids, files
  and thumbnails answer exactly like ones that never existed, and lists,
  search, counts, checks and raw records leave them out, apart from what
  "What hiding leaves in place" (in Hosted mode) lists.
- **Secrets stay on the server.** The storage query strings, the OAuth client
  secret and `MJV_AUTH_SECRET` never appear in a log line, a page, an API answer
  or an error message.
- **Headers.** Responses also carry `Strict-Transport-Security`; the Content
  Security Policy is the same as in local mode.
- **Signing out** clears the cookies in that browser. Sessions are not stored on
  the server, so to end every session at once, change `MJV_AUTH_SECRET`.
- **Logs** record sign-ins (with the email address), refused sign-ins (with the
  reason), failed requests (paths only), catalog problems, download results,
  deletions (one line each: the export id and who deleted it) and every hide
  or show again (one line each: the export id, who, and how many generations
  and moodboards it hides now; never an item's id, prompt or name).

Everyone who can sign in with your domain sees every published export, except
what its owner or an administrator hid; only they can delete one or hide parts
of it. The cache holds the downloaded ZIPs (`zips/`) and the thumbnails viewed
(`thumbs/`).

### Container image

`Dockerfile` builds a small image (Python 3.12 slim and Pillow, running as a
non-root user, listening on port 8000) that starts `python3 -m mjviewer --hosted`;
only `mjviewer/` is copied into it:

```sh
cd viewer
docker build -t mj-export-viewer .
docker run --rm -p 8000:8000 --env-file viewer.env mj-export-viewer
```

The platform in front of it must terminate TLS and pass the original `Host`
header. Give it a health check on `/healthz`.

### Try hosted mode locally

Everything runs on `127.0.0.1` against the test stand-ins for Google and the
storage; nothing is sent anywhere:

```sh
cd viewer
python3 -m tests.hosted_demo                      # two invented exports
python3 -m tests.hosted_demo export.zip ...       # publish ZIPs into a temporary fake container
python3 -m tests.hosted_demo --store DIR          # a folder holding catalog.json and exports/<sha256>.zip
```

It prints the address to open (port 8780 by default, `--port` to change it).
The fake Google signs in `alice@example.com` at once (`--email` for another
account, `--chooser` for an account page first, as Google shows). Ctrl+C stops
it and deletes its temporary folder; a `--store` folder is only read. In the
temporary container deleting is on and the signed-in account owns the ZIPs it
publishes (and the first invented export; the second has no owner), so
**Delete…** and **Hide…** can be tried there.

## Tests

```sh
cd viewer
python3 -m unittest -v
```

The unit tests (standard library only) build synthetic exports in temporary
folders (`tests/fixtures.py`) and cover the ZIP and folder readers and their
safety limits, parsing and relationship rules, the HTTP API and its security
checks, uploads, thumbnails (skipped without Pillow) and a check that crawling
every endpoint leaves an export byte-for-byte unchanged.

The hosted mode is tested against two local stand-ins: `tests/fake_google.py`
(Google's authorization, token and tokeninfo endpoints, with configurable
claims and failures, and an account page like Google's) and
`tests/fake_blob.py` (a private container that takes two fixed query strings,
read-only and read-write, and allows each only what its `sp` says; it answers
conditional writes and deletes as the storage does, and can inject 403, 404,
500, redirected, truncated, slow and length-less answers, write failures and a
concurrent writer). `tests/test_hosted.py` covers the settings, signing,
sign-in and every id-token rejection, sessions, the Host, Origin and token
rules on every route, request bodies on GET, who may delete what and the
checks on that request, who may hide what, a crawl of every route, page,
filter, search term, detail, file and thumbnail as someone who manages
nothing that must never meet a hidden item, and `python3 -m mjviewer --hosted`
as a process (start-up, every address, a clean stop during a download);
`tests/test_catalog.py` covers catalog validation and refresh, downloads and
their failures, explicit retries, two instances sharing one cache, eviction,
unpublishing, deleting (a concurrent writer, fields the viewer does not know,
storage failures, ZIPs already gone or kept, and every instance's copies),
hiding (hidden lists that cannot be read, the conditional rewrite, applying a
list before answering and on other instances, lists that change while an
export opens, builds that fail or outlive their export, and the limits), and
that the storage query strings never leak. `tests/test_hidden.py` covers what
a hidden list hides in a ZIP with one or two export roots and what the index
built without it leaves out, on a synthetic export whose hidden items carry a
marker in every id, name, code and file name. `tests/test_check.py` covers
`--check`.

An end-to-end browser test drives the real UI in Chromium on a synthetic
150-job export: lazy thumbnails and paging, search, filters, job pages and
lineage navigation, and a moodboard page. A hosted run signs in through the fake
Google, opens a published synthetic export, browses it, survives an ended
session and signs out; others follow a shared link through Google's account
page, and put two instances behind a strict round-robin front end
(`tests/hosted_harness.py`) to show that a failed export fails once.
`tests/test_e2e_delete.py` deletes a published export through the dialog
(cancelled with Esc and Cancel, a storage failure, then for real) and checks
that local mode shows none of it. `tests/test_e2e_hide.py` signs in, each in
a browser context of their own, as an export's owner, as an administrator and
as someone else: the owner hides a generation and a moodboard through their
dialogs (cancelled with Esc and Cancel, storage failures, then for real), and
shows them again from the Hidden section of the overview and from a hidden
item's banner; someone else sees no control and gets Not found for a hidden
item's link and pictures, and "Parent not available" on what was made from
it. It also covers a ZIP with two export roots, an instance with writes off,
a hidden list that cannot be applied, and local mode. All three are skipped
unless enabled:

```sh
python3 -m pip install --user playwright && python3 -m playwright install chromium
MJV_E2E=1 python3 -m unittest tests.test_e2e_browser tests.test_e2e_delete tests.test_e2e_hide -v
```

`tests/test_e2e_selection.py`, opt-in the same way, opens a synthetic export
in which the designer left moodboards, generations and a parent job out, and
checks how the overview, job pages and moodboard pages show them. Its browser
aborts every request that does not go to the viewer:

```sh
MJV_E2E=1 python3 -m unittest tests.test_e2e_selection -v
```

CI (`.github/workflows/check.yml`) runs the unit tests on Python 3.12, once
with Pillow (the version the container image installs) and once without.

## Layout

```text
viewer/
  README.md            this file
  API.md               the HTTP API (api 1) as implemented
  Dockerfile           the hosted-mode image (only mjviewer/ goes into it)
  mjviewer/
    __main__.py        command line
    sources.py         read-only, size-capped access to ZIPs and folders
    model.py           parsing, checks, links, lineage, facets, queries and hidden items
    server.py          HTTP server, security checks, files and thumbnails
    hosted.py          hosted mode: settings, sign-in, sessions, routes
    catalog.py         hosted mode: the published catalog and the verified ZIP cache
    check.py           --check
    static/            the single-page UI (plain JavaScript modules and CSS, no build)
  tests/               unittest suite, synthetic fixtures, fake Google and storage,
                       and the opt-in browser test
```

## Known limitations

- The date filter compares UTC calendar days, while cards show dates in your
  time zone, so a job made near midnight can appear on a different day than
  the one it filters under.
- Gallery cards stay in the page as you scroll (only their images load
  lazily); exports with tens of thousands of jobs work but are slower. A
  synthetic 60,000-job export loaded in about 7 seconds and took about 600 MB.
- In exports with more than 50,000 files, image types are checked when an image
  is first requested rather than at load time.
- ZIP entry names must use `/` and UTF-8, as the exporter's do; archives made by
  tools that write backslashes are refused.
- For folders, only the last path component is protected against a symbolic
  link swapped in while the viewer runs.
- In local mode the cache is never pruned; HTTP range requests and `HEAD` are
  not supported.
- Image dimensions are the exporter's metadata, not measured from the files.
- Previous and next on a job page work only when it was opened from the
  gallery.
- A job or moodboard whose id is `.` or `..` is listed but cannot be opened in
  the browser (it shows Not found), because browsers resolve those path
  segments before the request is sent.
- What the exporter does not export cannot be shown: videos, jobs deleted or
  hidden in Midjourney, earlier versions of a board, deleted boards, a board's
  full usage history, individual profile ratings, and likes of other creators'
  work.
- Tested in Chromium only.
- Hosted mode: a published export is downloaded whole before its first view
  (reading the ZIP from storage piece by piece was left out to keep the tested
  local reader), and every instance opens it in its own memory. Signing out
  does not revoke a copy of the session cookie; only a new `MJV_AUTH_SECRET`
  does. A changed label shows after a restart. Thumbnails are deleted only with
  their export. An export deleted through one instance can still be browsed
  through another one that has it open until that instance next reads
  `catalog.json` successfully (about a minute; at most 5 minutes, or 5 ×
  `MJV_CATALOG_TTL_S`, while its reads fail). A download that stalls inside a
  network read when the server stops leaves its temporary file and lock to the
  stale-file rules (another instance takes over after a minute).
- Hosted mode, hiding: an export that hides something is held twice in memory
  on each instance where it is open (the whole export, which its owner and the
  administrators need, and the copy without the hidden items that everyone
  else is served), so it takes about twice the memory: the synthetic
  60,000-job export took about 600 MB, and about 1.1 GB with a moodboard and
  100 generations hidden. Each hide or show again builds that copy anew on
  every instance where the export is open, about as long as opening it (about
  7 seconds for that export), unless what everyone else sees does not change
  (hiding on its own a generation already hidden with its moodboard); requests
  for that export wait meanwhile, and builds of several exports run one after
  another.
