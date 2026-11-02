# Architecture

## Responsibilities

- `src/page-bridge.js` runs in the page's main world. It learns the active own-feed identity, projects allowed response fields, authorizes image URLs observed in those records, and issues only fixed same-origin GET operations. It never reads cookies or request authentication headers.
- `src/content.js` runs in the isolated extension world. It owns explicit Scan/Export/Pause/Resume commands, scope selection, pagination, persistence, the on-page panel, and export status. It validates the current identity on each run and reports incomplete parents.
- `src/chooser.js` runs in the isolated world beside the controller and draws the "Choose what to export" window into the page: a full-viewport modal in a closed shadow root, so the page's CSS cannot reach it and its own CSS cannot reach the page. Prompts and names reach the DOM only as text. It reads `scan_data` and saves `selection` in `chrome.storage.local` itself; it makes no API requests. The controller holds the run lease from Choose's scan until the window closes, so no scan or export can start in any tab while a choice is open.
- `src/service-worker.js` mediates cross-tab run leases and Chrome downloads. Metadata blobs come from the extension's offscreen document; image URLs use the permitted CDN.
- `lib/` contains pure logic, shared with tests. `scripts/build.mjs` assembles these local modules into the classic content-script bundle. No remote bundler or runtime dependency is used.
- `ui/` provides the popup and offscreen metadata-blob lifecycle.

## Data and resume

Progress is local to the browser profile. Resume re-reads the selected own-feed scope to establish ownership; it does not blindly continue an old cursor after switching accounts. Completed downloads are checked against Chrome's download records and skipped. A changed scope needs New export; a new run leaves earlier downloaded folders intact.

Moodboard references and generations are separate datasets. `images` contains the board's current reference images; `generated_job_ids` links only selected feed jobs with an explicit matching personalization ID. Multiple codes from those references are retained in `codes`. Historical board snapshots and missing jobs cannot be reconstructed from those links.

## Choosing what to export

Scan keeps a compact copy of what the chooser needs under `scan_data`: the account id, the scope (`mode`, `max`, `since`, `testSubset`; not `images`), the scan time, each job's id, creation time, prompt text, kind, image count, parent id, own board ids and video flag, and each own board's id, name, reference image URLs and generation ids. A job belongs to every own board whose id equals one of its personalization reference ids; jobs linked to none are "Other generations". Small test does not trim `scan_data`: it trims the export.

The saved choice, `selection`, is `{user_id, scope, chosen_at, jobs: {id: bool}, moodboards: {id: bool}}` with a decision for every job and board seen when choosing. It is an opt-in snapshot: at export time an item is exported only when its decision is true. Items in scope that the choice does not mention appeared after it and are not exported (`not_reviewed` for jobs). A board's decision covers its row and reference images; its generations are decided one by one. A whole-board tick in the chooser is the board's decision plus every one of its generations.

Export and New export use the saved choice only when its account and scope match the run (N matters only for Max N, the date only for Since); otherwise they refuse before anything is written. A new run copies the choice into `run.selection`, and every attempt of that run, resumes included, uses that copy, never a newer choice. This is what keeps a folder consistent: a file downloaded by an earlier attempt is never left in a folder whose metadata leaves it out. A newer choice can still stop a resume: when the latest saved choice for the run's account, whatever its scope, is not the run's own (its `chosen_at` differs, or the run started without a choice) and marks false a job or moodboard the resume would export, the resume stops before it downloads or writes anything and leaves the saved run as it was. It checks the run's saved rows before it reads the feed, and the rows it would export after reading; items the newer choice does not mention do not stop it. New export then uses the newer choice, and clearing it lets the run finish with its own.

`applySelection` (`lib/selection.js`) turns the freshly read feed into what is exported. Without a choice the result is exactly the unchosen export, with `selection: null` in `export_meta.json`. With one, it keeps the chosen jobs; builds board rows for the chosen boards from the exported jobs only (`generated_job_ids`, `codes`, plus `generated_left_out`, the board's in-scope generations that were not exported); builds profile codes from the exported jobs only; reports parents that were in scope but left out as parent gaps with `left_out: true`; and summarizes the counts in the `selection` block. Small test then keeps the first exported board and profile. Downloads come only from the rows that remain, so nothing left out is downloaded.

The chooser's thumbnails are image loads made by the page, not by the extension: see [ENDPOINTS.md](ENDPOINTS.md#chooser-thumbnails).

## Trust and limits

Page messages and private API shapes are not a stable or privileged public API. The bridge has a fixed read-operation allowlist, but this is not a security audit or a guarantee against a compromised page. Active-page challenge detection is heuristic. Download errors expose categories rather than precise HTTP statuses.

Large archives have not been load-tested. Metadata is held in memory and persisted as a run object, so very large histories may require a future chunked/IndexedDB architecture. Folder output avoids ZIP size limits but does not remove that metadata-memory limitation. MV3 interruptions, removed download-history entries, and browser shutdown can require an explicit resume.

The technical read-only safeguards do not make automation permitted under Midjourney's terms. See the README's account-risk section before running.
