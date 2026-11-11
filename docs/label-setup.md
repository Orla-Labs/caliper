# Setting up a `caliper label` project

`caliper label` opens the start screen: **New project**, **Open project** or **Try the
demo**. A project is a folder holding `caliper.label.yaml` (commit it) and `.caliper-label/`
(labels and snapshots; ignored by git). Everything the wizard and Project settings do is saved
to that file, and hand edits to it are kept, comments and order included.

To switch projects, click the logo, the project name or the **Projects** tab. The start screen
then shows the open project with a *Continue labelling* link; starting or opening another
project leaves it as it is.

## The wizard

1. **Connect data.** Upload a file, give a local path, or connect S3, GCS, HTTP(S), Postgres,
   MySQL or SQLite. You see the row count, the first 50 rows and each column's detected type.
   Credentials stay in your environment: the project file only names the variable.
2. **Design the card.** Click columns to add them, drag to reorder, pick a display type:

   | Type | Shows | Detected from |
   |---|---|---|
   | `text`, `long_text` | the value as text | strings (long or multi-line: `long_text`) |
   | `number` | the number, with optional decimals | numeric columns |
   | `badge` | a small coloured tag | a few distinct short values |
   | `datetime` | the date / time in the labeller's local time (ISO in the tooltip) | `TIMESTAMP` / `DATE` columns, or text that is all ISO-8601 (`2026-11-06`, `2026-11-06T09:30:00Z`) |
   | `link` | a link | `http(s)://` and `s3://` / `gs://` values |
   | `image` | the image | URLs, object keys or paths ending in an image extension |
   | `json` | formatted JSON | nested columns, JSON text |

3. **Labels.** A type (single choice, multiple choice, yes/no/unsure, free text, score), then
   options with a colour and a hotkey. Templates: *Correct / Wrong / Unsure*,
   *True / Uncertain / False*, *Category list* (paste one per line), *1–5 rating*.
   - **From your data**: columns with a handful of values (say `priority`) are offered as
     ready-made options. If that column is also on the card, the wizard says so: labellers who
     see the current value tend to just agree with it.
   - **Colours mean something.** Verdict words get green / red / amber (Correct, Wrong,
     Unsure), neutral or other is slate, and scales such as low / medium / high / urgent or
     P0–P3 get a slate-to-red ramp in severity order. Other categories get colours that read
     neither good nor bad.
   - **Positive** (`approved: true` in the file) marks an answer that accepts the item, such as
     Correct. Only the eval exports use it: `golden` and `rollouts` score positive answers as 1.
     For plain categories such as a priority, leave it unticked. The wizard only warns about it
     when the project has a golden or rollouts export.
4. **Workflow.** Queues (one, one per value of a column, or one per `caliper sample` group),
   labels per item, assignment, markdown instructions and optional golden (quality-check) items.
5. **Review & start.** Name the project and pick its folder, check the YAML, create.

## Where new projects go

The folder field defaults to `<parent>/<project-name>`, where the parent is:

1. the parent folder of the last project you created;
2. else `$CALIPER_LABEL_PROJECTS`, if set;
3. else `~/caliper-projects`.

A candidate inside the git checkout the server was started from is skipped, so projects never
land in the Caliper source tree by accident. If you choose a folder inside any git checkout
yourself, the Review step warns you (only `caliper.label.yaml` would show up in its changes).
The last parent is kept in `$CALIPER_LABEL_HOME/prefs.json` (default `~/.caliper-label`).

## Setup API

Used by the start screen, the wizard and Project settings. In team mode, everything that
touches the filesystem or a new source answers only on the server machine.

| Route | What |
|---|---|
| `GET /api/projects` | recent projects, `demo_available`, `default_parent` (see above) |
| `POST /api/projects/open` `{path}` | open an existing project |
| `POST /api/projects/forget` `{path}` | drop it from the recent list |
| `POST /api/projects/demo` `{reset?}` | open the fictional demo project |
| `GET /api/setup/templates` | label templates |
| `POST /api/setup/templates/{id}` `{categories?, natural_order?}` | a `labels` block; `natural_order` sorts known scales |
| `GET /api/setup/connectors` | source kinds, including plugins |
| `PUT /api/setup/upload?filename=&draft=` | raw-body upload, staged until create |
| `POST /api/setup/preview` `{source, draft?, n?}` | credentials, columns with detected types, rows, count |
| `POST /api/setup/validate` `{config}` | `{issues, ok, yaml}` |
| `GET /api/setup/browse?path=&files=` | the folder / file picker |
| `POST /api/setup/check-dir` `{dir}` | `{git_root, exists, has_project, warnings}` for the folder field |
| `POST /api/setup/create` `{dir, config, draft?, overwrite?}` | write the project, open it, snapshot the items |
| `GET /api/settings` | the config, its YAML, revision, label counts, issues |
| `POST /api/settings/plan` `{config}` | dry run: option migrations and the YAML it would write |
| `PUT /api/settings` `{config, revision, confirm}` | save; removing an option with labels retires it |

Errors are `{"detail": ..., "hint": ...}`.
