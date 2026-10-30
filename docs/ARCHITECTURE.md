# Architecture

## Responsibilities

- `src/page-bridge.js` runs in the page's main world. It learns the active own-feed identity, projects allowed response fields, authorizes image URLs observed in those records, and issues only fixed same-origin GET operations. It never reads cookies or request authentication headers.
- `src/content.js` runs in the isolated extension world. It owns explicit Scan/Export/Pause/Resume commands, scope selection, pagination, persistence, the on-page panel, and export status. It validates the current identity on each run and reports incomplete parents.
- `src/service-worker.js` mediates cross-tab run leases and Chrome downloads. Metadata blobs come from the extension's offscreen document; image URLs use the permitted CDN.
- `lib/` contains pure logic, shared with tests. `scripts/build.mjs` assembles these local modules into the classic content-script bundle. No remote bundler or runtime dependency is used.
- `ui/` provides the popup and offscreen metadata-blob lifecycle.

## Data and resume

Progress is local to the browser profile. Resume re-reads the selected own-feed scope to establish ownership; it does not blindly continue an old cursor after switching accounts. Completed downloads are checked against Chrome's download records and skipped. A changed scope needs New export; a new run leaves earlier downloaded folders intact.

Moodboard references and generations are separate datasets. `images` contains the board's current reference images; `generated_job_ids` links only selected feed jobs with an explicit matching personalization ID. Multiple codes from those references are retained in `codes`. Historical board snapshots and missing jobs cannot be reconstructed from those links.

## Trust and limits

Page messages and private API shapes are not a stable or privileged public API. The bridge has a fixed read-operation allowlist, but this is not a security audit or a guarantee against a compromised page. Active-page challenge detection is heuristic. Download errors expose categories rather than precise HTTP statuses.

Large archives have not been load-tested. Metadata is held in memory and persisted as a run object, so very large histories may require a future chunked/IndexedDB architecture. Folder output avoids ZIP size limits but does not remove that metadata-memory limitation. MV3 interruptions, removed download-history entries, and browser shutdown can require an explicit resume.

The technical read-only safeguards do not make automation permitted under Midjourney's terms. See the README's account-risk section before running.
