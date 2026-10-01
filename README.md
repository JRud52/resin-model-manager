# Resin Model Manager

A small self-hosted web app for a resin printing library. It indexes STL, LYS and
CTX files (also CTB, OBJ, 3MF), including files inside `.zip` and `.7z` archives,
groups them by **release** and **model**, separates **supported** and
**unsupported** versions, and shows rendered previews of the STLs.

![Library grid](docs/grid.png)

## How it works

* **Import, never touch the originals.** Your existing library is mounted
  read-only at `/source`. *Import library* copies it into `/library` (skipping
  files that are already copied and unchanged), and the app only ever works on
  that copy. Archives stay archived, so you keep the space savings.
* **Grouping.** Each file's path is read with archives treated as folders
  (`Release/Heroes.zip` → `Release/Heroes/...`). The top folder is the release
  (or set `RELEASE_DEPTH=1` if you keep `Creator/Release/...`). The first
  meaningful folder below it is the model; folders like `Supported`, `STL`,
  `32mm`, `Lychee` or `<Model> Presupported` are skipped as noise. Deeper
  folders such as `Heads` or `Weapon options` become option groups. Loose files
  are grouped by their shared name prefix (`Orc_Warboss_Body.stl` +
  `Orc_Warboss_Axe.stl` → *Orc Warboss*).
* **Supported / unsupported** comes from folder and file names (`supported`,
  `presupported`, `pre-supp`, `_sup`, `unsupported`, `no supports`, ...). An
  unmarked STL sitting next to `<same name>_supported` counts as unsupported.
* **Corrections.** The guesses will sometimes be wrong. *Edit* on a model renames
  it or moves it to another release (renaming onto an existing model merges
  them); ✎ on a file changes its support flag, model, release or option group,
  for just that file or for a whole folder. Corrections are stored as rules on
  the path, survive rescans, and can be removed under ⚙. No files are renamed.
* **Tags.** Add tags on a model's page (type and press Enter; existing tags are
  suggested). With a release selected, *Tag all models in this release* adds or
  removes tags on every model in it at once. Click tags in the sidebar to filter
  (several tags = models that have all of them). The search box also matches
  tags; `tag:painted` matches that tag exactly. Tags follow a model when you
  rename or merge it.
* **Previews.** STLs are rendered by a built-in CPU renderer (numpy, no GPU or
  OpenGL needed) and cached as WebP images in `/data/previews`. Files inside
  archives are rendered straight from the archive without extracting them into
  the library; 7z archives are unpacked once into `/data/tmp` per batch and
  cleaned up. Previews render in the background after a scan (model covers
  first), and on demand when you open something not rendered yet. For LYS/CTX
  files the app shows an embedded thumbnail when the file has one.
* **3D view.** *Rotate in 3D* loads the STL in the browser with three.js (this
  needs internet access from the browser, not from the NAS).

![Model view](docs/model.png)

## Running on the NAS

GitHub Actions builds the image on every push to `main` and publishes it as
`ghcr.io/jrud52/resin-model-manager:latest` (amd64 and arm64). Nothing in
`docker-compose.yml` needs editing: folder paths, user and port come from
environment variables (see `.env.example` for the full list).

The first time the workflow runs, GitHub creates the package as private. Make
it public once (GitHub → your profile → **Packages** → `resin-model-manager` →
**Package settings** → **Change visibility**) so the NAS can pull it without a
login.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SOURCE_PATH` | `/volume1/3d-models` | Your existing library, mounted read-only at `/source`. |
| `LIBRARY_PATH` | `/volume1/resin-manager/library` | New empty folder for the imported copy. |
| `DATA_PATH` | `/volume1/resin-manager/data` | New empty folder for the database and preview cache. |
| `PUID` / `PGID` | `1000` | Your NAS user (`id -u`, `id -g`), so copied files belong to you. |
| `PORT` | `8080` | Port on the NAS. |

### With Portainer (auto-updates from Git)

1. **Stacks → Add stack → Repository.** Repository URL
   `https://github.com/JRud52/resin-model-manager`, reference `refs/heads/main`,
   compose path `docker-compose.yml`.
2. Under **Environment variables**, add `SOURCE_PATH`, `LIBRARY_PATH`,
   `DATA_PATH`, `PUID` and `PGID` (plus any settings below you want to change).
3. Turn on **GitOps updates**, mechanism **Polling** (e.g. every `5m`), and turn
   on **Re-pull image** and **Force redeployment**. Force redeployment makes
   every poll pull the image even when the repo has no new commit, so an image
   that finishes building a few minutes after the commit is still picked up.
   The container is only recreated when the image actually changed.
4. **Deploy the stack**, open `http://<nas>:8080` and click **Import library**.

Don't use a compose file with a `build:` section in Portainer: it builds once
and then keeps reusing the old image on updates.

### With docker compose

1. Copy `docker-compose.yml` and `.env.example` to the NAS, rename the latter to
   `.env` and fill in your paths and IDs.
2. `docker compose up -d`, then open `http://<nas>:8080`.
3. Update later with `docker compose pull && docker compose up -d`.

To build the image yourself instead of pulling it, run
`docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build`
from a checkout.

### First run

Click **Import library**. Progress shows in the header. Rendering previews for
a large library takes a while the first time; after that they come from the
cache.

Once you trust it, you can point `/library` at your real library instead of the
copy (keep it `:ro` if you like; the app only writes to `/data`, except during
import) and skip the import step.

### Settings (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `RELEASE_DEPTH` | `0` | Folder levels above the release folder (`1` for `Creator/Release/...`). |
| `PREVIEW_WORKERS` | `1` | Background render threads. Raise on a stronger NAS. |
| `PRERENDER` | `1` | `0` renders previews only when you look at them. |
| `PREVIEW_SIZE` | `512` | Preview image size in pixels. |
| `MAX_PREVIEW_MB` | `1024` | Skip previews for STLs larger than this. |
| `SCAN_INTERVAL_MINUTES` | `0` | Rescan the library on a timer (0 = only when you click Rescan). |

Changing `RELEASE_DEPTH` takes effect on the next rescan.

## Limits

* RAR archives and archives nested inside archives are not read.
* LYS/CTX files only get a preview if they contain an embedded image.
* Password-protected archives are skipped (shown as errors in the server log).

## Development

```sh
pip install -r requirements.txt
python tests/make_sample_library.py /tmp/rmm/src
SOURCE_DIR=/tmp/rmm/src LIBRARY_DIR=/tmp/rmm/lib DATA_DIR=/tmp/rmm/data uvicorn app.main:app --port 8080
python tests/smoke_test.py http://localhost:8080 /tmp/rmm/src
```

Stack: Python 3.12, FastAPI, SQLite, numpy + Pillow renderer, py7zr; plain
HTML/JS front end with no build step.

## License

MIT, see [LICENSE](LICENSE).
