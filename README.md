# CoomerDL (TesticularMass fork)

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Platforms](https://img.shields.io/badge/Windows%20%7C%20Linux-from%20source-blue)
![License](https://img.shields.io/github/license/TesticularMass/CoomerDL)
![Last commit](https://img.shields.io/github/last-commit/TesticularMass/CoomerDL)

# Notice

1. CoomerDL only downloads what a site already serves publicly. It cannot bypass paywalls, logins or private content.
2. Coomer and Kemono block downloaders. Use **coomerfans.com** and **pawchive.pw** instead (see [Supported sites](#supported-sites)).
3. This is a personal fork. Report problems with it here, not on the original project.

## Table of contents

- [Notice](#notice)
- [Description](#description)
- [What this fork adds](#what-this-fork-adds)
- [Supported sites](#supported-sites)
- [Installation](#installation)
- [How to use](#how-to-use)
- [Settings](#settings)
  - [Download folder](#download-folder)
  - [Download database](#download-database)
  - [Duplicate removal](#duplicate-removal)
  - [File name templates](#file-name-templates)
  - [SimpCity cookies](#simpcity-cookies)
  - [Logs](#logs)
- [Coming from upstream CoomerDL](#coming-from-upstream-coomerdl)
- [Translations](#translations)
- [Issues](#issues)
- [Feature requests](#feature-requests)
- [Disclaimers](#disclaimers)
- [Credits](#credits)

## Description

CoomerDL is a desktop app that downloads images, videos and files from supported sites. Paste a profile, post, album or thread URL, pick a folder, and it downloads everything with progress bars, retries, a record of what you already have, and duplicate removal.

It is built with Python and a **PySide6 (Qt)** interface.

This project started as a fork of [Emy69/CoomerDL](https://github.com/Emy69/CoomerDL). Since then the codebase has been simplified (dead code and duplicated downloader logic removed, one shared batch runner for every site), several download bugs have been fixed, and new options have been added for where and how files are saved. The commit history has the details.

## What this fork adds

| Feature | Upstream | This fork |
|---|---|---|
| Download folder in Settings | Main window only | ✅ Both, kept in sync |
| Skip files already downloaded | Always on | ✅ Can be switched off |
| Database location | Fixed | ✅ Any file, with copy or switch |
| Duplicate removal (XXH3 content hash) | — | ✅ Per model |
| File naming | 4 fixed modes | ✅ Fixed modes plus custom templates with subfolders |
| Same name, different file | Overwritten | ✅ Kept as `name (2).ext` |
| Bunkr files recognised on re-runs | — (signed URLs change every visit) | ✅ |
| Paid "full version" upsell | Yes | No |

## Supported sites

| Site | Status | Notes |
|------|--------|-------|
| [coomerfans.com](https://coomerfans.com/) | ✅ Working | Alternative to Coomer |
| [pawchive.pw](https://pawchive.pw/) | ✅ Working | Alternative to Kemono |
| [erome.com](https://www.erome.com/) | ✅ Working | Albums and profiles |
| [bunkr](https://bunkr-albums.io/) | ✅ Working | Any bunkr domain (bunkr.si, bunkr.site, etc.) |
| [simpcity.cr](https://simpcity.cr/) | ✅ Working | Includes GoonBox images and turbo.cr videos posted in threads. May require [cookies](#simpcity-cookies) |
| ~~coomer.st~~ | ❌ Blocked | Use **coomerfans.com** |
| ~~kemono.cr~~ | ❌ Blocked | Use **pawchive.pw** |
| ~~jpg5.su~~ | ❌ Not working | Downloads currently fail |

If you paste a URL from a blocked site, the app tells you which working alternative to use instead of trying the download.

## Installation

This fork has no prebuilt releases yet, so run it from source. You need **Python 3.10+**. Dependencies come from `requirements.txt`: PySide6, requests, beautifulsoup4, cloudscraper (SimpCity only) and xxhash.

```bash
git clone https://github.com/TesticularMass/CoomerDL.git
cd CoomerDL
python -m venv .venv
```

Activate the virtual environment:

```bash
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

# Linux / macOS
source .venv/bin/activate
```

Install and run:

```bash
pip install -r requirements.txt
python main.py
```

Start the app from the project folder. Settings, the database and logs are stored under `resources/config/` relative to where it was launched.

## How to use

1. Launch the application
2. Paste a URL from a supported site
3. Select your download folder (main window or **Settings > Downloads**)
4. Choose the content types you want (images, videos, compressed files)
5. Click **Download**

Files are organised into a folder per model, then by type (`images`, `videos`, `documents`, `compressed`, `other`).

### Several profiles in one session

1. Paste one or more URLs (separated by spaces or new lines) and click **Add**
2. Each queued profile shows its status (pending, downloading, completed, error)
3. Click **Download**: profiles run **one at a time** and the progress bar shows *Profile 2 of 3*
4. When the session ends, a summary lists what completed and what failed, and why

A failed profile doesn't stop the session. **Cancel** stops the current profile and skips the pending ones. Pending profiles can be removed with the ✕ button or the Delete key. The queue lives in memory only. A session allows **3 profiles** by default; change it in **Settings > Downloads > Max Profiles per Session**.

## Settings

Open **Settings** from the main window. Each change is saved when you click that tab's **Apply** button and is kept across restarts.

- **General**: language
- **Downloads**: download folder, simultaneous downloads, profiles per session, retries, retry interval, folder structure, file naming mode or custom template, skipping already downloaded files, duplicate removal
- **Cookies**: SimpCity cookies (import, save, clear) with a built-in tutorial
- **Database**: database file location, browse records by user and post, search, totals, export, delete records

### Download folder

Set it from the main window's **Select Folder** button or from **Settings > Downloads > Download Folder**. Both show the same folder, and it is created if it doesn't exist.

### Download database

CoomerDL records every downloaded file in a local SQLite database and skips files it has already recorded. A file is recorded **only after it has fully downloaded**, so cancelled or failed downloads are retried next time. Turn skipping off with **Skip files already in the download database**.

The default location is `resources/config/downloads.db`. In **Settings > Database > Database file** you can:

- pick a **new** file: the app offers to copy your current records into it (the old file is kept) or start empty
- pick an **existing** database: the app switches to it, which is handy for keeping separate libraries

Downloads that are already running finish on the previous file; the new location applies from the next download.

### Duplicate removal

Every file is hashed with **XXH3** (128-bit) while it downloads, which adds almost no time. With **Remove duplicate files within each model** enabled, a new file whose content matches a file already in the same model's folder is deleted, and its URL is recorded against the file you kept so it is not downloaded again.

A "model" is the top-level folder a download goes into: the user folder for coomerfans and pawchive, or the album or thread folder for erome, bunkr and SimpCity. Identical files under different models are kept.

Files downloaded before hashing existed are hashed once, the first time that model is downloaded again. On a large video library that first run pauses briefly while it does this.

### File name templates

Choose **Custom template** as the file naming mode, then write a template. The settings window shows a live example and rejects unknown tokens.

| Token | Value |
|---|---|
| `{name}` | Original file name without extension |
| `{ext}` | Extension with the dot, e.g. `.jpg` |
| `{index}` | Attachment number within the post |
| `{title}` | Post or album title |
| `{post_id}` | Post ID |
| `{published}` | Post date and time as given by the site |
| `{date}` | Post date as YYYY-MM-DD |
| `{user}` | Model / user name or ID |
| `{site}` | Site the file comes from |
| `{type}` | images, videos, documents, compressed or other |
| `{url_hash}` | Short hash of the file URL |

Examples:

```text
{date} - {title}_{index}{ext}      ->  2024-05-01 - Beach day_1.jpg
{date}/{title}_{index:03}          ->  2024-05-01/Beach day_001.jpg
{post_id}_{name}                   ->  123456_a1b2c3d4.jpg
```

- `/` creates subfolders inside the model's media folder
- format specs work, e.g. `{index:03}` for zero padding
- the extension is added if the template leaves it out
- characters that aren't allowed in file names are replaced with `_`
- if two different files get the same name, the second becomes `name (2).ext` instead of overwriting the first

### SimpCity cookies

SimpCity may require cookies depending on the content or your session. In **Settings > Cookies** you can paste cookies as JSON, import them from a file, or clear them. They are only used for SimpCity.

### Logs

The app shows logs tagged by site and can export them:

```text
bunkr: Resolving /f/ URL ...
coomer: Duplicate of dl/model/images/a_1.jpg, removed the new copy from https://...
system: Download settings were applied successfully.
```

Exported logs go to `resources/config/logs/`.

## Coming from upstream CoomerDL

You can drop in your existing `resources/config/` folder:

1. **settings.json** is read as is. New options start at their defaults (skipping and duplicate removal on, naming mode unchanged).
2. **downloads.db** is upgraded automatically on the first download (a hash column is added). Your records are kept, so files you already have are still skipped.
3. Existing files are hashed for duplicate checks the next time each model is downloaded.
4. Bunkr files downloaded with upstream were recorded under signed URLs that never match again, so they may download once more; duplicate removal deletes those repeat copies.

## Translations

English and Español are maintained; Türkçe is a community translation. Files live in `resources/config/i18n/`, and any key missing from a language falls back to English.

To add a language:

1. Copy `en.json` to your language code (e.g. `fr.json`)
2. Translate the **values only** and keep placeholders such as `{url}`, `{error}`, `{path}` unchanged
3. Register it in `languages.json` under `community`
4. Select it in **Settings > General**

## Issues

Open an issue in [this repository](https://github.com/TesticularMass/CoomerDL/issues). Include:

- the URL type (profile, post, album, thread) and site
- what you expected and what happened
- the exported log from `resources/config/logs/`

Reports without a log are hard to act on and may be closed.

## Feature requests

Feature requests are welcome as [issues](https://github.com/TesticularMass/CoomerDL/issues). Describe what you're trying to do, not just the button you want.

## Disclaimers

1. This tool is not affiliated with, endorsed by or sponsored by any of the sites it supports. All trademarks belong to their owners.
2. You are responsible for how you use it, including respecting the terms of the sites you download from and the rights of content creators. The software is provided as is, without warranty (see [LICENSE](LICENSE)).

## Credits

- Original app by [Emy69](https://github.com/Emy69/CoomerDL), released under the MIT License. Emy69 also maintains [Coomer CLI](https://github.com/Emy69/Coomer-cli) and [SimpCity CLI](https://github.com/Emy69/SimpCityCLI).
- Fork maintained by [TesticularMass](https://github.com/TesticularMass).
- Translators and contributors are listed in the commit history.
