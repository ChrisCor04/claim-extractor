# estimate-extractor

A multi-carrier property-insurance estimate PDF extractor. It converts a
carrier's estimate PDF (State Farm, Travelers, USAA, Farmers/Mid-Century,
Allstate, or a generic Xactimate-style layout) into a stable,
carrier-agnostic **canonical JSON** representation, with full provenance,
confidence scoring, and validation/reconciliation reporting.

```
Carrier Estimate PDF
        |
Page classification
        |
Text/table extraction
        |
Optional OCR fallback
        |
Carrier-aware parsing
        |
Canonical estimate JSON  (extraction stage -- see below)
        |
Validation and reconciliation
        |
Normalizer               (mapping stage -- see docs/mapping-engine.md)
        |
Normalized estimate JSON
        |
Matcher + scorer
        |
Mapped estimate JSON
        |
Mapping validator
        |
Mapping report + review CSV
        |
Local Review UI          (Phase 3 -- see docs/local-review-ui.md)
        |
Verified Xactimate catalog builder  (Phase 3.5 -- see docs/verified-catalog-builder.md)
        |
Approved estimate JSON + review history
        |
Automation input JSON + approved line items CSV
```

## Windows Setup — New Computer

This section is for someone setting up Claim Extractor on a Windows PC for
the first time, including with little or no Python experience. It covers
the local review UI and the Xactimate desktop automation (Fast Grouped).
If you only want the PDF-extraction CLI on macOS/Linux, skip to
[Installation](#installation) instead.

**Overview:**

1. Install prerequisites (Python, Tesseract OCR)
2. Download Claim Extractor
3. Run `setup-windows.ps1`
4. Start Claim Extractor
5. Calibrate Xactimate on this computer
6. Run a test estimate

**Calibration is per computer.** Xactimate's on-screen layout differs by
monitor, DPI, and window size, so Claim Extractor measures it fresh on
each machine and stores the result outside the repository, keyed to that
machine. A new computer must calibrate itself — never copy a calibration
file from another machine (see [step 5](#5-first-time-xactimate-calibration)).

**All commands in this guide are PowerShell commands, not Command
Prompt.** Windows 10/11 both include PowerShell by default — open the
Start menu and search for **"Windows PowerShell"** (not "Command
Prompt" / "cmd"). The `.\something.ps1` syntax used throughout this
guide is PowerShell-specific and will not work the same way in
Command Prompt.

### 1. Install prerequisites

#### Python

Claim Extractor requires **Python 3.11 or newer** (`pyproject.toml`
declares `requires-python = ">=3.11"`). `setup-windows.ps1` looks for
Python 3.13, then 3.12, then 3.11 via the `py` launcher, in that order.
This has been validated end-to-end (clean venv, dependency install, and
import test) on **Python 3.12**.

1. Download Python from the official source: <https://www.python.org/downloads/windows/>
   Any 3.11, 3.12, or 3.13 release works; 3.12 is the version this setup
   has been directly validated against.
2. Run the installer. **Check "Add python.exe to PATH"** on the first
   installer screen — this is what makes the `py` launcher and `python`
   command work from PowerShell afterward.
3. Verify the install in a **new** PowerShell window:
   ```powershell
   py --version
   ```
   This should print `Python 3.11.x`, `3.12.x`, or `3.13.x`. If PowerShell
   says `py` (or `python`) is not recognized, re-run the installer and
   make sure "Add python.exe to PATH" is checked, or open a new terminal
   window (PATH changes don't apply to already-open windows).

Do not install a version older than 3.11 — nothing in this repository has
been validated against it, and `pyproject.toml` will refuse the install.

#### Tesseract OCR

**What it is:** Tesseract is a free, local OCR (optical character
recognition) engine. Claim Extractor's Xactimate automation uses it to
read on-screen text in the Xactimate window (group names, field labels,
etc.) — it never sends screenshots anywhere; everything stays on your
computer.

**Why Claim Extractor needs it:** the Xactimate automation layer
(`windows_adapter.py`) calls `pytesseract` unconditionally the first time
it needs to read anything on screen. Without a working Tesseract install,
calibration and Fast Grouped execution cannot function — this is not the
same as the *optional* OCR fallback described later in
[Optional OCR setup](#optional-ocr-setup) for the PDF-extraction pipeline.

**Exactly how Claim Extractor finds it (verified against the current
code):** at startup of any Xactimate automation feature, the app checks
whether `C:\Program Files\Tesseract-OCR\tesseract.exe` exists.
- If it exists, that exact path is used directly — no PATH entry needed.
- If it does not exist at that exact path, the app falls back to
  `pytesseract`'s own default behavior, which looks for a program named
  `tesseract` on your system `PATH`.

There is **no environment-variable override** for this specific lookup
(unlike the `TESSERACT_CMD` mention in
[Optional OCR setup](#optional-ocr-setup) below, which applies only to
the separate PDF-extraction OCR fallback, not to the Xactimate automation
path) — install to the default location and you won't need to think
about this further.

**Installation steps:**

1. Download the Windows installer from
   [UB-Mannheim's Tesseract build](https://github.com/UB-Mannheim/tesseract/wiki)
   (the standard, widely-used Windows distribution of Tesseract).
2. Run the installer and **accept the default installation directory**
   (`C:\Program Files\Tesseract-OCR`). This matches exactly what Claim
   Extractor checks for automatically — no PATH changes needed.
3. Verify the file exists:
   ```powershell
   Test-Path "C:\Program Files\Tesseract-OCR\tesseract.exe"
   ```
   This should print `True`.
4. Verify it actually runs, by invoking it at its full path (it is not on
   `PATH` by default, so a bare `tesseract --version` will typically say
   "not recognized" even after a correct install — this is expected):
   ```powershell
   & "C:\Program Files\Tesseract-OCR\tesseract.exe" --version
   ```
   This should print a version line such as `tesseract v5.4.0...`.

If you install Tesseract somewhere other than the default directory, you
must add that folder to your system `PATH` yourself and confirm a bare
`tesseract --version` works from PowerShell — otherwise the Xactimate
automation will not find it.

#### Xactimate desktop

Claim Extractor automates your **existing, already-installed** Xactimate
desktop application — it does not install, license, or replace Xactimate
in any way. Before continuing:

- Xactimate desktop must already be installed on this computer.
- It must be licensed and opening normally on its own.
- You should be able to open a project in it manually before asking
  Claim Extractor to control it.

This guide does not cover obtaining, installing, or licensing Xactimate —
that's between you and Xactware. Claim Extractor's code does not check
for or require any specific Xactimate version — it works by reading and
controlling whatever version's window is currently on screen.

### 2. Download Claim Extractor

> **This repository currently requires GitHub access to clone or
> download — it is not public.** This was confirmed by querying GitHub's
> API for this repository while logged out, which returned "Not Found"
> (the standard response GitHub gives for a private repository when
> you're not authenticated — a public repository would return its
> details instead). This means:
> - The person setting up this second computer needs a **GitHub
>   account**, and that account needs to be **added as a collaborator**
>   on this repository (done from GitHub's website, by whoever currently
>   owns/administers it — not something this README can do for you).
> - Once added, they'll need to **sign in to GitHub** the first time
>   they clone or download — either in the browser (for the ZIP option)
>   or via a sign-in prompt that Git itself opens (for the Git option,
>   described below). Neither option requires manually creating or
>   pasting a token/password in most cases — modern Git for Windows and
>   the browser both handle this with a normal GitHub login screen.
> - If this repository is later made public, this whole notice — and the
>   sign-in step below — no longer applies, and a plain `git clone`
>   works with no login.

Pick a **short** install path, such as `C:\claim-extractor`, rather than
somewhere deeply nested (e.g. inside several levels of `Documents\Work\Projects\...`).
Windows has a default 260-character path limit, and this was directly
observed during setup validation — a dependency install failed purely
because of an overly long folder path, with no other problem involved.
A short path avoids that entirely.

**Option A — Git.** Preferable if you're willing to install one extra
tool, because future updates are then a single `git pull` (see
[Updating later](#8-updating-later)) instead of re-downloading a new ZIP
each time.

**Do you already have Git?** Open PowerShell and run:

```powershell
git --version
```

If that prints something like `git version 2.xx.x.windows.1`, Git is
already installed — skip to "Clone the repository" below.

**If PowerShell says `git` is not recognized, install Git for Windows:**

1. Download the installer from the official source:
   <https://git-scm.com/download/win>
2. Run the installer. The default options are sufficient for this
   guide — you only need `git clone`/`git pull`, and the installer's
   default settings already include adding `git` to your `PATH` and
   installing Git Credential Manager (which is what will show you a
   normal GitHub sign-in window when needed, described above). You do
   not need to change any installer screen for this setup to work.
3. Close and reopen PowerShell (a window opened before installing won't
   see the update), then verify:
   ```powershell
   git --version
   ```
   This should now print a version number.

**Clone the repository:**

```powershell
cd C:\
git clone https://github.com/ChrisCor04/claim-extractor
cd claim-extractor
```

What each line actually does:
- `cd C:\` moves PowerShell to the root of your `C:` drive — everything
  after this happens relative to there.
- `git clone https://github.com/ChrisCor04/claim-extractor` downloads
  the repository and **automatically creates a new folder**
  `C:\claim-extractor` for you — you do not need to (and should not)
  create that folder yourself first. If a sign-in window appears here
  (see the access note above), complete it; the clone will continue
  automatically afterward.
- `cd claim-extractor` moves PowerShell **into** that newly created
  folder. After this command, your PowerShell prompt should show you're
  working inside `C:\claim-extractor` — that's where you'll run the
  remaining commands in this guide.

> **Note on the URL:** `https://github.com/ChrisCor04/claim-extractor`
> comes from this repository's currently configured `origin` remote. If
> you intend to share this repository from a different (e.g. renamed or
> org-hosted) location, confirm this is the URL the recipient should
> actually use before sending these instructions.

**If something goes wrong:**
- **`git : The term 'git' is not recognized...`** — Git isn't installed
  or PowerShell wasn't reopened after installing it. See the install
  steps above.
- **`fatal: destination path 'claim-extractor' already exists and is
  not an empty directory`** — you (or a previous attempt) already
  created/cloned into `C:\claim-extractor`. Either `cd C:\claim-extractor`
  to reuse it if it looks complete, or remove that folder first if it's
  a stray leftover, then re-clone.
- **`fatal: could not create work tree dir... Permission denied`** (or
  similar, right after `cd C:\`) — some machines (especially
  company-managed ones) restrict write access to the `C:\` root for
  standard (non-Administrator) accounts. Don't run this as Administrator
  just to work around it — instead, clone into a folder you already own,
  e.g.:
  ```powershell
  cd $env:USERPROFILE
  git clone https://github.com/ChrisCor04/claim-extractor
  cd claim-extractor
  ```
  This creates `C:\Users\<you>\claim-extractor` instead — still short
  enough to avoid the path-length problem above, and always writable by
  your own account.

**Option B — ZIP download (no Git required).** Simpler if you don't want
to install Git — but you'll need to download a fresh ZIP for any future
update rather than running one `git pull`, unless a future version of
this project adds its own updater.

1. In your browser, sign in to GitHub if you aren't already (see the
   access note above), then open this repository's GitHub page:
   <https://github.com/ChrisCor04/claim-extractor>
2. Click the green **Code** button.
3. Click **Download ZIP**.
4. Wait for the download to finish.
5. In File Explorer, open your **Downloads** folder and find the
   downloaded file (typically `claim-extractor-main.zip`).
6. If you want it somewhere other than Downloads, move or copy the ZIP
   there now (e.g. to `C:\`) — doing this before extracting is usually
   simplest.
7. Right-click the ZIP file and choose **Extract All...**, then choose a
   **short** destination (e.g. `C:\`) and confirm.
8. GitHub ZIPs normally extract into a folder named after the repository
   and branch, e.g. **`C:\claim-extractor-main`** — the exact name can
   differ slightly (a different branch or version could produce a
   different suffix). Open that folder and confirm it contains files
   including `README.md`, `setup-windows.ps1`, `start-windows.ps1`, and
   `requirements-windows.txt` — if you see those, you're in the right
   place.
9. Open PowerShell **inside that folder**: in File Explorer, open the
   folder, click the address bar, type `powershell`, and press Enter.
   (Alternatively, from any PowerShell window: `cd C:\claim-extractor-main`,
   adjusting the path to match whatever folder name you actually got in
   step 8.)

ZIP users should **not** run `git clone` — you already have the files.
The remaining steps in this guide (`setup-windows.ps1` and onward) are
identical either way.

### 3. Run setup

From inside the repository folder in PowerShell:

```powershell
.\setup-windows.ps1
```

**If PowerShell refuses to run it** (a message about scripts being
disabled), see [Troubleshooting](#7-troubleshooting) — do not
permanently change your machine's security settings to work around this.

This does not require Administrator/elevated PowerShell — it only
creates a folder and installs packages inside the repository you already
own, the same as any normal `pip install`.

**What this script does, exactly** (read `setup-windows.ps1` directly if
you want to verify this yourself before running it):
1. Checks you're on Windows.
2. Looks for a Python 3.13/3.12/3.11 interpreter via the `py` launcher
   (falling back to a plain `python` on PATH if it's 3.11+).
3. Creates a `.venv` folder in the repository — only if one doesn't
   already exist; it never deletes or recreates an existing `.venv`.
4. Installs `requirements-windows.txt` (the UI + Xactimate automation
   dependencies) and then the repository itself, editable, into that
   `.venv`.
5. Prints next steps.

**What it explicitly does NOT do:**
- It does not install, configure, or launch Xactimate.
- It does not run a claim or touch any project data.
- It does not copy calibration from another machine — calibration
  doesn't exist yet until you do it yourself in step 5.
- It does not change Fast Grouped, calibration, or any other application
  behavior — it only installs Python packages and the repo itself.

A successful run ends by printing `Setup complete.` followed by the next
steps listed in the script. The exact pip output above that will vary
run to run (package download order, cached-vs-downloaded, etc.) — don't
worry if it doesn't look identical between machines, as long as no red
`ERROR` lines appear and the script reaches `Setup complete.`.

### 4. Start Claim Extractor

```powershell
.\start-windows.ps1
```

This runs the repository's own entrypoint
(`.\.venv\Scripts\python.exe -m estimate_extractor ui`) using the `.venv`
that setup just created — it does not add any behavior of its own.

The UI binds to `127.0.0.1` only (localhost) — consistent with the
existing CLI's own documented behavior ("Binds to 127.0.0.1 only — never
0.0.0.0") — so it is reachable only from this computer, never from your
network.

After starting, you should see console output ending with a local URL,
and your browser should open (or you can open it manually) to
`http://127.0.0.1:8501`, showing the **ClaimXtract** app with a sidebar
("Workspace: Quick Run / Advanced") and a title reading "Add a claim PDF
and execute it in Xactimate. Everything stays on this computer." If your
browser doesn't open on its own, opening that URL manually in any
browser works the same way.

You may see a Windows Defender Firewall prompt the first time you start
it — this can happen even for a localhost-only app on some Windows
configurations. It's safe to allow it; the app only listens on
`127.0.0.1` regardless of how you answer.

**Starting order:** it doesn't matter whether you start Claim Extractor
or Xactimate first — Xactimate only needs to be open and showing a
project once you get to calibration or executing a plan, not just to
launch this UI.

**Stopping it:** press `Ctrl+C` in the PowerShell window `start-windows.ps1`
is running in (or simply close that window). **Restarting it:** run
`.\start-windows.ps1` again.

### 5. First-time Xactimate calibration

> **IMPORTANT — every new computer must be calibrated.** Calibration
> measures where things are on *this* screen, at *this* window size and
> DPI. It is stored outside the repository, named after a hash of this
> machine's own hostname, and the app will not find or reuse a
> calibration file from a different computer even if you tried to copy
> one over — and doing so would silently corrupt automation if it somehow
> matched a file name, since the coordinates would describe a different
> screen.

With Xactimate open and Claim Extractor running (Workspace: **Quick
Run**):

1. In Xactimate's **Grouping** panel, create **3 empty groups** named
   exactly:
   ```
   CAL_ROW_ALPHA
   CAL_ROW_BRAVO
   CAL_ROW_CHARLIE
   ```
   Keep all three visible as consecutive rows in the Grouping panel (they
   can stay empty).
2. In Claim Extractor, under **Xactimate calibration setup**, click
   **Calibrate Xactimate**.
3. Claim Extractor measures your current Xactimate layout automatically —
   no further input from you. On success you'll see **"Calibration
   complete — Ready for Fast Execution"**, and the **Saved Xactimate
   calibration** panel above it will show **Status: Ready** along with
   your measured client size, DPI, and row pitch.

If the button reports missing or incomplete groups, it will tell you
exactly which of the three names it found and which are still missing —
create the missing ones and click **Calibrate Xactimate** again.

**Where it's stored:** `%LOCALAPPDATA%\ClaimExtractor\xactimate_calibrations\`,
in a file named after a hash of this computer's hostname — entirely
outside the repository folder, so it survives a `git pull` and is never
committed or shared by cloning the repo.

**It survives restarting the app and rebooting the computer** — it's an
ordinary file on disk, not tied to the running process. You only need to
repeat calibration when something about the Xactimate window's on-screen
geometry has actually changed: a different monitor, a different display
scaling/DPI setting, or the Xactimate window being resized. If you move
Xactimate to a different monitor with different DPI/scaling, or change
Windows display scaling, re-calibrate before your next Fast Grouped run —
the app detects this mismatch (see [Troubleshooting](#7-troubleshooting),
"Needs calibration") rather than silently using stale coordinates.

### 6. First test

Before running an important live claim on a new computer, validate the
whole path end-to-end with something low-stakes:

1. Launch Claim Extractor (`.\start-windows.ps1`) and open Xactimate.
2. Use a disposable/**TEST** Xactimate project — not a real client file —
   for this first run.
3. Complete calibration (step 5) if you haven't already.
4. Add a small, known estimate PDF via **Add claim and run → Add file**.
5. Use **Generate / refresh fast grouped plan**, review the plan Claim
   Extractor shows you, then **Execute approved fast grouped plan**.
6. Watch Xactimate: confirm groups are created, then confirm several
   individual line items appear correctly (right category/selector/
   quantity) as execution proceeds.
7. Confirm the run finishes without errors, matching what you saw on the
   screen.

**While a Fast Grouped run is executing, don't touch your mouse or
keyboard, and don't click into Xactimate yourself.** The automation
drives Xactimate with real, synthetic mouse and keyboard input — anything
you do at the same time can land in the wrong place and corrupt the run
in progress. Wait for it to finish before interacting with the screen
again.

Only move on to a real claim once this completes cleanly.

### 7. Troubleshooting

| Symptom | Least-invasive fix first |
|---|---|
| `py` / `python` "is not recognized" | Reopen PowerShell in a new window (PATH changes need a fresh window). If it still fails, reinstall Python and check "Add python.exe to PATH". |
| `git` "is not recognized" | Install Git for Windows (default options) from <https://git-scm.com/download/win>, then reopen PowerShell. See [step 2](#2-download-claim-extractor). |
| You're using Command Prompt (`cmd.exe`) instead of PowerShell | Open **Windows PowerShell** from the Start menu instead — `.\setup-windows.ps1`-style commands are PowerShell syntax. |
| Cloning/downloading asks you to sign in, or fails with a permission/access error on GitHub | This repository currently requires GitHub access — see the note at the top of [step 2](#2-download-claim-extractor). Confirm your GitHub account has been added as a collaborator. |
| `git clone` fails with "Permission denied" right under `C:\` | Don't run as Administrator to force it — clone into `$env:USERPROFILE` instead (a folder you already own). See [step 2](#2-download-claim-extractor). |
| `destination path '...' already exists` | A folder from a previous attempt is already there. Reuse it (`cd` into it) if it looks complete, or delete the stray folder and re-clone. |
| PowerShell won't run `setup-windows.ps1` (execution policy error) | Run it via `powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1` — this affects only that one invocation, nothing permanent. See details below. |
| PowerShell says the file "is blocked" / came from another computer | The ZIP download may carry Windows' "Mark of the Web". Run `Unblock-File .\setup-windows.ps1` (and optionally every file: `Get-ChildItem -Recurse \| Unblock-File`), then try again. |
| Dependency install fails partway through | Re-run `.\setup-windows.ps1` — pip resumes/redownloads as needed. If it fails at the *same* package every time, note the exact error before asking for help. |
| Install fails mentioning a very long filename/path | You're likely inside a deeply nested folder. Move the whole repository to a short path such as `C:\claim-extractor` and re-run setup. |
| "Could not invoke tesseract" / Xactimate automation errors mentioning OCR | Confirm `Test-Path "C:\Program Files\Tesseract-OCR\tesseract.exe"` returns `True`. If Tesseract is installed elsewhere, add its folder to `PATH` and confirm `tesseract --version` works directly. |
| `tesseract --version` says "not recognized" right after installing | Expected if you accepted the default install directory — Tesseract isn't on `PATH` by default. Verify with the full-path command in [step 1](#tesseract-ocr) instead; only add it to `PATH` if you installed elsewhere. |
| Xactimate not detected | Confirm Xactimate is open, licensed, and showing a project (not a splash/login screen) before clicking Calibrate Xactimate or executing a plan. |
| "Not calibrated" / "Calibration required" | Expected on a brand-new computer — follow [step 5](#5-first-time-xactimate-calibration). |
| "Needs calibration" after previously showing Ready | Something about the Xactimate window changed (size, DPI, moved monitor). Re-calibrate on this machine — do not reuse a profile from before the change. |
| UI doesn't start / browser shows nothing | Check the PowerShell window setup/start ran in for an error. Confirm you're visiting `http://127.0.0.1:8501` (not `0.0.0.0`). |
| Windows Defender Firewall prompt appears on first start | Safe to allow — the app only listens on `127.0.0.1` (this computer only) regardless of how you answer the prompt. |
| "Port already in use" / port 8501 busy | Another Claim Extractor instance (or something else) is already using that port. Close it, or start with a different port: `.\.venv\Scripts\python.exe -m estimate_extractor ui --port 8502`. |

**About the execution-policy fix specifically:** Windows' default
`RemoteSigned` policy blocks unsigned scripts that were downloaded from
the internet, but does not require permanently loosening your machine's
security to run one script once. `powershell -ExecutionPolicy Bypass
-File .\setup-windows.ps1` only bypasses the policy for that single
process — it does not change any persistent setting on your machine.
Avoid running `Set-ExecutionPolicy` at the `LocalMachine` or
`CurrentUser` scope just to get this script running.

### 8. Updating later

If you set up with Git:

```powershell
git pull
.\setup-windows.ps1
```

Re-running `setup-windows.ps1` after a `git pull` is safe: it reuses your
existing `.venv` rather than deleting it (it only creates one if missing)
and simply re-installs/updates packages from `requirements-windows.txt`
into it. It has no knowledge of, and does not touch, your saved Xactimate
calibration, which lives outside the repository entirely.

---

## Purpose and non-goals

This project has four internal stages, all offline and all living in
this one repository.

**Extraction** answers: *what does the uploaded insurance estimate document
actually say?* It converts a carrier PDF into a stable, carrier-agnostic
canonical JSON representation with full provenance and confidence scoring.
It does not interpret or classify line items in any Xactimate-aware way.

**Mapping** (Phase 2, `src/estimate_extractor/mapping/`) answers: *given
what was extracted, what trade/action/component concept is this, and which
verified Xactimate catalog entry (if any) does it match?* It reads only
`canonical_estimate.json`, never the PDF, and never alters an extracted
fact. See [docs/mapping-engine.md](docs/mapping-engine.md) for the full
pipeline, normalization rules, scoring, and catalog format.

**Local Review UI** (Phase 3, `src/estimate_extractor/ui/`) answers: *which
mapping suggestions has a human actually verified, and what's safe to hand
to automation?* A local-only Streamlit app for uploading PDFs, running the
above two stages, correcting/approving mapping suggestions, building up a
verified Xactimate catalog over time, and exporting only approved,
fully-qualified line items. See
[docs/local-review-ui.md](docs/local-review-ui.md).

**Verified Catalog Builder** (Phase 3.5, layered into `src/estimate_extractor/ui/`)
answers: *has a human actually confirmed this exact category/selector in
Xactimate, or are we still guessing?* A reviewer manually transcribes what
they personally verified in their own licensed Xactimate selector
browser -- category, selector, description, unit, activity symbol,
price-list context -- once per distinct item; ClaimXtract then reuses that
verified record automatically for every future compatible item, under
strict trade/component/unit/action compatibility checks (never on text
similarity alone). See
[docs/verified-catalog-builder.md](docs/verified-catalog-builder.md).

**Canonical Selector Database** (Phase 3.6, `src/estimate_extractor/selector_catalog/`)
answers: *what Category/Selector/Description combinations does Xactimate
actually define?* A permanent, local, offline-searchable reference index
(SQLite + CSV + JSON) built once by OCR'ing reviewer-supplied Xactimate
selector-browser screenshots -- not a pricing database, not a bulk copy of
Xactimate's catalog, just Category/Selector/Description with full
provenance back to the source screenshot. It's a lookup tool a reviewer
searches *while* doing Phase 3.5's verification work, not a replacement
for it. See [docs/selector-catalog.md](docs/selector-catalog.md).

All five parts deliberately do **not**:

- invent Xactimate CAT/SEL codes without a verified source (see
  "Xactimate data integrity" in docs/mapping-engine.md),
- generate an ESX file or drive any desktop automation,
- adjudicate coverage, interpret policy language, or recommend a
  repair scope or depreciation outcome.

Those remain out of scope for a future automation stage; keeping them out
is what keeps both the canonical schema and the mapping output stable
enough for that stage to build on.

Everything runs **fully offline** -- no network calls, no LLM, no paid API
required at runtime, on both macOS and Windows.

## Supported carriers

| Carrier | Adapter key | Notes |
|---|---|---|
| State Farm | `state_farm` | |
| Travelers | `travelers` | |
| USAA | `usaa` | Per-line Overhead & Profit column; often no "Claim Number" label (Member Number + L/R Number instead) |
| Farmers / Mid-Century | `farmers` | |
| Allstate | `allstate` | No per-line tax column; descriptions frequently wrap around the numeric block |
| *(fallback)* | `generic` | Used when no carrier crosses the detection confidence threshold (default 0.70) |

See [docs/carrier-adapters.md](docs/carrier-adapters.md) for how adapters
work and how to add a new carrier.

## Architecture

```
src/estimate_extractor/
  pdf/            Layer 1/2/3: native text extraction (PyMuPDF), word/line
                   layout, the Xactimate-row tokenizer, and the optional
                   OCR fallback (pytesseract + Tesseract, disabled by default)
  classification/ Page classification (carrier-agnostic) + carrier detection
  adapters/       One CarrierProfile (detection keywords + column schema)
                   per carrier, built on a shared BaseCarrierAdapter
  parsing/        Carrier-agnostic parsing: claim metadata, coverages,
                   sections/areas, line items, notes, totals, and the
                   cross-page continuation state machine
  normalization/  Decimal-safe money/date/unit/text normalization
  validation/     Structural rules + arithmetic reconciliation ->
                   extraction_report.json
  output/         canonical_estimate.json / line_items.csv /
                   extraction_report.json / document_pages.json writers
  models/         Pydantic v2 canonical schema (+ JSON Schema export)
  pipeline.py     Wires the above into one run_extraction() call
  mapping/        Phase 2: normalizer, action/trade/component detectors,
                   Xactimate catalog, deterministic scorer/matcher, mapping
                   validator, pipeline, output writers -- see
                   docs/mapping-engine.md
  ui/             Phase 3 (+3.5): local Streamlit review UI --
                   project/pipeline/review/catalog/export services, plus
                   Phase 3.5's verified_catalog_service.py (stable
                   selector identity + price observations),
                   group_name_service.py, and project_context_service.py
                   -- see docs/local-review-ui.md and
                   docs/verified-catalog-builder.md
  selector_catalog/ Phase 3.6: OCR pipeline (image inventory, table-region
                   detection, row parsing, deduplication, validation,
                   SQLite persistence, exporters) that builds the
                   permanent Category/Selector/Description reference
                   database from screenshots -- see docs/selector-catalog.md
  cli.py          extract / map / process / ui / catalog / selectors / validate / inspect
```

Config files driving the mapping stage live in `config/` (not hardcoded in
Python): `normalization_rules.yaml`, `mapping_catalog.yaml`,
`mapping_scoring.yaml`, plus Phase 3.5's `verified_xactimate_catalog.yaml`,
`xactimate_group_names.yaml`, `xactimate_activity_symbols.yaml`.
`config/backups/` holds timestamped backups written before every catalog
edit (both the Phase 3 placeholder catalog and the Phase 3.5 verified
catalog).

## Installation

Requires **Python 3.11+**.

### macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

If your system Python is older than 3.11 (check with `python3 --version`),
install a newer one first, e.g. via Homebrew:

```bash
brew install python@3.12
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
```

### Windows (PowerShell)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .
```

### Optional OCR setup

OCR (Layer 3) is disabled by default and only used for pages with little or
no native text, and only when you pass `--enable-ocr`. It uses local
Tesseract (no cloud service, no API key):

```bash
pip install -r requirements-dev.txt   # installs pytesseract + Pillow
```

Then install the Tesseract binary itself:

- **macOS**: `brew install tesseract`
- **Windows**: install from
  [UB-Mannheim's Tesseract build](https://github.com/UB-Mannheim/tesseract/wiki)
  and ensure `tesseract.exe` is on `PATH`, or set the `TESSERACT_CMD`
  environment variable to its full path.

### Optional local review UI setup

The Phase 3 review UI (Streamlit) is optional and only needed for `python
-m estimate_extractor ui`:

```bash
pip install -r requirements-ui.txt   # installs streamlit + pandas
```

## CLI usage

```bash
# Single file
python -m estimate_extractor extract "fixtures/Aranda Insurance.pdf"

# A directory, recursively
python -m estimate_extractor extract fixtures --recursive

# With OCR fallback enabled
python -m estimate_extractor extract "fixtures/Odom Insurance.pdf" --enable-ocr

# Validate a previously generated canonical_estimate.json against the schema
python -m estimate_extractor validate "output/aranda-insurance/canonical_estimate.json"

# Inspect one page's classification + row-token breakdown (debugging aid)
python -m estimate_extractor inspect "fixtures/Garrety Insurance Estimate.pdf" --page 5

# Map an already-extracted canonical_estimate.json (mapping stage alone)
python -m estimate_extractor map "output/aranda-insurance/canonical_estimate.json"

# Full pipeline: PDF -> extract -> normalize -> map -> validate -> outputs
python -m estimate_extractor process "fixtures/Aranda Insurance.pdf"
python -m estimate_extractor process fixtures --recursive

# Launch the local review UI at http://127.0.0.1:8501 (binds to localhost only)
python -m estimate_extractor ui
python -m estimate_extractor ui --projects-dir ./projects --port 8501

# Inspect/search/validate the verified Xactimate catalog (Phase 3.5)
python -m estimate_extractor catalog list
python -m estimate_extractor catalog search "pipe jack"
python -m estimate_extractor catalog validate
python -m estimate_extractor catalog stats
python -m estimate_extractor catalog export-review
python -m estimate_extractor catalog restore-latest

# Build/query the canonical Xactimate selector database (Phase 3.6)
python -m estimate_extractor selectors import fixtures/reference/ClaimXtract_Xactimate_Master_Selector_Source_v1.zip
python -m estimate_extractor selectors validate
python -m estimate_extractor selectors stats
python -m estimate_extractor selectors search "ridge vent"
python -m estimate_extractor selectors search --category RFG "pipe jack"
python -m estimate_extractor selectors export-csv
python -m estimate_extractor selectors review-queue
```

Options for `extract`: `--output-dir`, `--enable-ocr`, `--carrier
{state_farm,travelers,usaa,farmers,allstate,generic}` (force an adapter
instead of auto-detecting), `--strict` (treat any warnings as exit code 1
even without errors), `--debug` (always write per-page debug JSON),
`--log-level`, `--overwrite/--no-overwrite`, `--recursive`,
`--redact-debug-output`, `--config <path>`. `process` accepts the same
options plus mapping-stage output; `map` accepts `--output-dir`; `ui`
accepts `--projects-dir` and `--port`. `catalog` subcommands are
read/audit/recovery tooling only -- creating or confirming a verified
record is a UI (or service-layer) action; see
[docs/verified-catalog-builder.md](docs/verified-catalog-builder.md).
`selectors import` is resumable (`--force` to reprocess) and accepts
`--category`/`--limit` to scope a run; see
[docs/selector-catalog.md](docs/selector-catalog.md).

Exit codes: `0` success · `1` completed with review warnings (extraction or
mapping) · `2` extraction failure · `3` invalid arguments · `4`
dependency/configuration problem.

See [docs/mapping-engine.md](docs/mapping-engine.md) for what `map`/`process`
write, how normalization and scoring work, and the Xactimate
data-integrity policy (no invented CAT/SEL codes); see
[docs/local-review-ui.md](docs/local-review-ui.md) for the `ui` command,
project structure, and review/approval/export workflow.

### Example output (real run against the fixture set)

```
$ python -m estimate_extractor extract "fixtures/Aranda Insurance.pdf"
Processing Aranda Insurance.pdf ...
  Carrier: State Farm (1.00)
  Pages: 15
  Estimate pages: 11
  Excluded pages: 4
  Coverages: 2
  Sections: 9
  Line items: 42
  Warnings: 1
  Reconciliation: PASS
  Status: success
  Output: output/aranda-insurance/
```

```
output/aranda-insurance/
  canonical_estimate.json
  extraction_report.json
  line_items.csv
  document_pages.json
  raw_text/page_001.txt ... page_015.txt
  debug/page_001.json ... page_015.json
```

Running `process` instead of `extract` additionally writes, in the same
directory, the four mapping-stage outputs described in
[docs/mapping-engine.md](docs/mapping-engine.md):

```
output/aranda-insurance/
  normalized_estimate.json
  mapped_estimate.json
  mapping_report.json
  mapping_review.csv
```

## Schema

See [docs/canonical-schema.md](docs/canonical-schema.md) for the full
field-by-field explanation (provenance pattern, depreciation notation,
IDs, known limitations) and
[`schemas/canonical_estimate.schema.json`](schemas/canonical_estimate.schema.json)
for the machine-readable JSON Schema.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

This runs the extraction unit suite (money/date/unit/text normalization,
the row tokenizer, carrier detection, page classification including the
instructional-sample guard, arithmetic reconciliation, output
serialization, cross-page continuation, coverage attribution), the mapping
unit suite (action/trade/component detection, normalization, catalog
validation, scoring/matching including conflict caps and tie handling,
mapping validation), the Phase 3 UI-service unit suite (project creation
and duplicate detection, review-state persistence and field overrides,
approval-rule validation, bulk actions, catalog-rule validation/backup/
restore, approved-estimate and automation-export generation including
export filtering), the Phase 3.5 verified-catalog unit suite
(category+selector compound uniqueness and exact punctuation preservation,
verification-confirmation gating, screenshot-transcribed vs. human-verified
status, matching safety including unit/action/negative-pattern conflict
handling and suffix-sensitive selectors, item-only vs. reusable-rule
verification, no-silent-overwrite-of-approved-items, catalog backup/
restore/audit, group-name suggestion/alias/fuzzy-match, project-context
confirmation gating), and four integration suites: extraction against all
six real fixture PDFs checked against hand-verified golden files in
`tests/expected/`, the full extract-to-map pipeline against those same six
fixtures (asserts no line item ever disappears or is altered by mapping),
the full extract-to-map-to-review pipeline through the UI service layer
against those same six fixtures (create project, approve/reject/correct
items, export, reopen from disk, confirm persistence), and the full
verified-catalog workflow against those same six fixtures (synthetic,
clearly-labeled test-only verified record improves matching for compatible
items only, unrelated items and prior approvals stay untouched, exports
stay verified-and-approved-only) -- see
[docs/local-review-ui.md](docs/local-review-ui.md) and
[docs/verified-catalog-builder.md](docs/verified-catalog-builder.md)
"Tests". Streamlit rendering itself isn't unit tested (not feasible
without a browser); only the service layer it calls is.

It also runs the Phase 3.6 selector-catalog unit suite (selector-
punctuation preservation, folder-vs-title-bar category detection and
mismatch flagging, row grouping/column assignment, truncation detection,
cross-screenshot deduplication including near-duplicate-vs-genuine-
conflict handling, validator completion invariants, SQLite persistence
and search, resumable-import behavior against a fake OCR engine) and,
when the reference screenshot library is present locally, an integration
suite that runs the real, local Tesseract pipeline against real
screenshots from all eight required categories (RFG, ELE, PLM, PNT, WTR,
FNC, WDV, XST), including the one real, verified folder/title-bar
mismatch in the library -- see
[docs/selector-catalog.md](docs/selector-catalog.md) "Known limitations"
for the OCR-accuracy tradeoffs this surfaced.

**Fixture PDFs contain real customer PII and are gitignored** -- integration
tests skip gracefully (not fail) if `fixtures/originals/*.pdf` aren't
present in your checkout (see `fixtures/supplements/` for the matching
supplement PDFs, not currently wired into any test). The Phase 3.6
reference screenshot library (`fixtures/reference/`) is similarly
gitignored (large, Xactimate-proprietary-adjacent) and its integration
tests skip gracefully if `fixtures/reference/extracted/` isn't present.

Run every fixture through the CLI and print a summary table:

```bash
python scripts/run_all_fixtures.py
python scripts/summarize_results.py
```

## Troubleshooting

See [docs/troubleshooting.md](docs/troubleshooting.md).

## Privacy

- No network calls, no telemetry, nothing is uploaded anywhere.
- Ordinary logs are redacted (emails/phones/ZIPs stripped) at INFO level
  and above (`logging_config.py:RedactingFilter`).
- Full raw page text is written only to local `debug/*.json` /
  `raw_text/*.txt` output files; pass `--redact-debug-output` to redact
  those too.
- **Fixture PDFs and generated `output/` must never be committed to a
  (especially public) repository** -- both are excluded via `.gitignore`.
  Do not put real customer data in README screenshots, issue reports, or
  test fixtures beyond what's already gitignored.
- The Phase 3 review UI binds to `127.0.0.1` only (never `0.0.0.0`), makes
  no network calls, and stores everything under `projects/` and
  `config/backups/` -- both gitignored, both contain PII. See
  [docs/local-review-ui.md "Privacy"](docs/local-review-ui.md#privacy).

## Known limitations

Full detail in [docs/canonical-schema.md "Known limitations"](docs/canonical-schema.md#known-limitations)
and [docs/troubleshooting.md](docs/troubleshooting.md); summary:

- `coverage_id` on sections/line-items is `null` when a document has
  multiple coverages and no unique, evidence-backed attribution could be
  found (`parsing/coverage_attribution.py` only assigns a `coverage_id`
  when an exact-sum partition against the printed coverage totals has
  exactly one solution; ties or no-match cases stay `null` with an
  explanatory note, never a guess). Verified against real fixtures: Garcia
  and Odom fully resolve and match their documents' own "Recap by Room"
  tables; Aranda, Bagi, and Wei Tang correctly stay `null`.
- Reconciliation excludes "Paid When Incurred" / code-upgrade line items
  from the primary calculated total (reported separately as
  `paid_when_incurred_total`) and still reports `FAIL` with an explanatory
  `note` for the remaining cases where per-coverage totals can't be
  reconciled against a single document-wide reported total.
- The mapping stage's Xactimate catalog (`config/mapping_catalog.yaml`) is
  intentionally sparse -- only the roofing category is populated, and no
  selector is populated at all, since no licensed Xactimate price list is
  in this repository. See
  [docs/mapping-engine.md](docs/mapping-engine.md#xactimate-data-integrity-critical-constraint).
- `bounding_boxes`/`line_ranges` on line items are empty; only page-level
  word bounding boxes are captured (Layer 1/2 text-stream parsing, not full
  2-D spatial table reconstruction).
- Area/section nesting is inferred from label-line adjacency heuristics
  (see docs/canonical-schema.md #2), verified against the fixture set by
  hand but not guaranteed for an unseen carrier template.
- A handful of pages across the fixture set land in `unknown`
  classification (flagged as an info-level issue, excluded from the
  estimate body) rather than being force-classified.
- OCR (Layer 3) uses plain `image_to_string`/`image_to_data` Tesseract
  output with no image preprocessing (deskew, binarization); scanned pages
  with poor scan quality may still extract poorly. It was implemented and
  is behind a clean `OCREngine` protocol, but was not exercised against a
  real scanned fixture (none of the six fixtures are image-only).
- The Phase 3 review UI's "reveal project folder" and pipeline
  stage-progress labels are both best-effort (OS-native open command;
  coarse before/after markers rather than live sub-step progress) -- see
  [docs/local-review-ui.md "Known limitations"](docs/local-review-ui.md#known-limitations).
- The verified Xactimate catalog (`config/verified_xactimate_catalog.yaml`)
  ships with **zero** `human_verified` records -- only 58 non-production
  `screenshot_transcribed` ACT-category (acoustical treatments) rows used
  to exercise the architecture. Building real automation-ready coverage
  requires actual reviewer time in a licensed Xactimate environment. See
  [docs/verified-catalog-builder.md "Known limitations"](docs/verified-catalog-builder.md#known-limitations).
- The canonical selector database (Phase 3.6) is built by local OCR and
  inherits OCR's known limitations -- in particular, character-confusion
  misreads (`I`/`1`, `O`/`0`) are not heuristically corrected (doing so
  risks silently inventing a wrong selector code), and a misread of the
  selector code itself (rather than the description) across two
  screenshots of the same real item produces two separate database
  records rather than one merged one. See
  [docs/selector-catalog.md "Known limitations"](docs/selector-catalog.md#known-limitations).

## Next-step roadmap

1. Use the Verified Catalog tab (Phase 3.5), searching the new canonical
   selector database (Phase 3.6, `selectors search`) as a reference while
   doing it, to build real `human_verified` records from a licensed
   Xactimate environment -- one reviewer verification at a time, reused
   automatically for every future compatible item under strict
   compatibility checks. See
   [docs/verified-catalog-builder.md](docs/verified-catalog-builder.md)
   and [docs/selector-catalog.md](docs/selector-catalog.md).
2. Grow `config/mapping_catalog.yaml` (the Phase 2 placeholder catalog)
   similarly, from an authoritative, documented source -- never guessed
   (see [docs/mapping-engine.md](docs/mapping-engine.md#xactimate-data-integrity-critical-constraint)).
3. Xactimate desktop automation that consumes `automation_input.json`
   once a meaningful number of selectors have been human-verified
   (explicitly out of scope for this repository at this stage; Phases 3
   and 3.5 exist specifically to make that verification process
   tractable and safe, not to build the automation itself).
4. Coverage attribution for the remaining unresolved documents, if a
   reliable signal can be found beyond the printed text (e.g. accepting
   the source ESX/XML alongside the PDF, when available, as a secondary
   input).
5. Full spatial (word-bounding-box-driven) table reconstruction as a
   fallback for carriers whose linearized text stream doesn't follow the
   one-field-per-line convention this MVP relies on.
6. Broader carrier coverage as more sample documents become available.
