from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Semaphore
from urllib.parse import parse_qsl, urlparse
import glob
import os
import random
import re
import requests
import threading
import time
import zlib
import sqlite3

import xxhash

from downloader.core.filename_template import (
    DEFAULT_TEMPLATE,
    TemplateError,
    date_from_published,
    render_template,
)

# Mode value for the user-defined filename template; 0-3 are the presets.
CUSTOM_TEMPLATE_MODE = 4
HASH_CHUNK_SIZE = 1048576
# XXH3 is far faster than SHA-256; the 128-bit variant costs the same as the
# 64-bit one and makes an accidental match (which deletes a file) negligible.
new_hasher = xxhash.xxh3_128
# Hex digest length; stored hashes of another length predate XXH3.
HASH_HEX_LENGTH = 32
DEFAULT_DB_PATH = os.path.join("resources", "config", "downloads.db")
# Longest file or folder name written, in UTF-8 bytes. Filesystems allow
# 255; the margin leaves room for the " (2)" collision suffix and the
# ".<thread id>.tmp" name used while downloading.
MAX_FILENAME_BYTES = 200
# Post titles inside generated names are cut to this many bytes so the
# index and hash that follow them survive.
MAX_TITLE_BYTES = 120
_TMP_SUFFIX_RE = re.compile(r"\.\d+\.tmp")


class DownloadCancelled(Exception):
    """Raised inside a download when the user cancels it."""


class DownloadError(Exception):
    """A download failure whose message is already translated."""


class BaseApiDownloader:
    # Temporary files being written by any downloader in this process;
    # anything else matching the temporary name pattern is a leftover
    # from a run that was killed and can be deleted.
    _active_tmp_paths = set()
    _active_tmp_lock = threading.Lock()

    def __init__(
        self,
        download_folder,
        max_workers=5,
        log_callback=None,
        enable_widgets_callback=None,
        update_progress_callback=None,
        update_global_progress_callback=None,
        headers=None,
        max_retries=3,
        retry_interval=1.0,
        download_images=True,
        download_videos=True,
        download_compressed=True,
        tr=None,
        folder_structure="default",
        rate_limit_interval=1.0,
        db_path=None,
    ):
        self.download_folder = download_folder
        self.log_callback = log_callback
        self.enable_widgets_callback = enable_widgets_callback
        self.update_progress_callback = update_progress_callback
        self.update_global_progress_callback = update_global_progress_callback
        self.cancel_requested = threading.Event()
        self.headers = headers or {
            "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
            "Referer": "https://coomer.st/",
            "Accept": "text/css",
        }
        self.session = requests.Session()
        self.max_workers = max_workers
        self.per_domain_limit = 2
        self.executor = ThreadPoolExecutor(max_workers=self.max_workers)
        self.domain_locks = defaultdict(lambda: Semaphore(self.per_domain_limit))
        self.domain_last_request = defaultdict(float)
        self.rate_limit_interval = rate_limit_interval

        self.video_extensions = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".wmv", ".m4v")
        self.image_extensions = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff")
        self.document_extensions = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx")
        self.compressed_extensions = (".zip", ".rar", ".7z", ".tar", ".gz")

        self.download_images = download_images
        self.download_videos = download_videos
        self.download_compressed = download_compressed

        self.futures = []
        self.total_files = 0
        self.completed_files = 0
        self.skipped_files = []
        self.failed_files = []
        # Set when a profile or thread listing could not be fully loaded,
        # so the session can report the download as partial.
        self.incomplete_reason = None
        self.tr = tr
        self.shutdown_called = False
        self.folder_structure = folder_structure
        self.max_retries = max_retries
        self.retry_interval = retry_interval
        self.file_lock = threading.Lock()
        self.counter_lock = threading.Lock()
        self.post_attachment_counter = defaultdict(int)
        self.request_timeout = (10, 120)
        # Query parameters that carry the original filename (bunkr uses n).
        self.filename_query_keys = ("n",)

        # Overwritten from settings by the controller after construction.
        self.file_naming_mode = 0
        self.file_name_template = DEFAULT_TEMPLATE
        self.skip_downloaded = True
        self.dedupe_by_hash = True
        self.duplicate_files = []
        self._template_error_logged = False
        self._hash_backfill_done = set()
        self._backfill_lock = threading.Lock()

        self.domain_error_state = defaultdict(
            lambda: {
                "burst_count": 0,
                "last_error_ts": 0.0,
                "cooldown_until": 0.0,
            }
        )
        self.domain_error_lock = threading.Lock()
        self.domain_error_window = 10.0
        self.domain_error_threshold = 4
        self.domain_cooldown_seconds = 8.0

        self.db_path = db_path or DEFAULT_DB_PATH
        db_folder = os.path.dirname(self.db_path)
        if db_folder:
            os.makedirs(db_folder, exist_ok=True)
        self.db_lock = threading.Lock()
        self.init_db()
        self.load_download_cache()
        self.domain_name = "system"

    def _translate_text(self, key, **kwargs):
        if callable(self.tr):
            try:
                return self.tr(key, **kwargs)
            except TypeError:
                key = self.tr(key)
        return key.format(**kwargs) if kwargs else key

    def init_db(self):
        self.db_connection = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db_cursor = self.db_connection.cursor()
        self.db_cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                media_url TEXT UNIQUE,
                file_path TEXT,
                file_size INTEGER,
                user_id TEXT,
                post_id TEXT,
                downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                file_hash TEXT
            )
            """
        )
        # Databases created before hashing existed lack the column.
        self.db_cursor.execute("PRAGMA table_info(downloads)")
        columns = {row[1] for row in self.db_cursor.fetchall()}
        if "file_hash" not in columns:
            self.db_cursor.execute("ALTER TABLE downloads ADD COLUMN file_hash TEXT")
        self.db_cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_downloads_file_hash ON downloads(file_hash)"
        )
        self.db_connection.commit()

    def load_download_cache(self):
        with self.db_lock:
            self.db_cursor.execute("SELECT media_url, file_path, file_size FROM downloads")
            rows = self.db_cursor.fetchall()
        self.download_cache = {row[0]: (row[1], row[2]) for row in rows}

    def log(self, message, **kwargs):
        final_message = self._translate_text(message, **kwargs)
        domain = getattr(self, "domain_name", "system")
        if self.log_callback:
            self.log_callback(domain, final_message)

    def sanitize_filename(self, filename):
        return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename)

    @staticmethod
    def _fit_bytes(name, max_bytes=MAX_FILENAME_BYTES, keep_ext=True):
        """Shorten a name to max_bytes of UTF-8, keeping its extension."""
        if len(name.encode("utf-8")) <= max_bytes:
            return name
        stem, ext = os.path.splitext(name) if keep_ext else (name, "")
        if len(ext.encode("utf-8")) > 16:
            # Not a real extension, just a dot inside a long name.
            stem, ext = name, ""
        budget = max_bytes - len(ext.encode("utf-8"))
        stem = stem.encode("utf-8")[:budget].decode("utf-8", "ignore").rstrip(" .")
        return (stem or "file") + ext

    def _safe_relative_path(self, name):
        """
        Turn a generated or server-supplied name into a relative path that
        cannot leave the target folder: every component is sanitized and
        shortened, and empty, "." and ".." components are dropped.
        """
        parts = []
        for raw in re.split(r"[\\/]+", str(name or "")):
            part = self.sanitize_filename(raw).strip().rstrip(" .")
            if part.strip("."):
                parts.append(self._fit_bytes(part))
        return os.path.join(*parts) if parts else "file"

    def _safe_filename(self, name):
        """A single file name, without any folder part the server sent."""
        base = re.split(r"[\\/]+", str(name or ""))[-1]
        return self._safe_relative_path(base)

    def request_cancel(self):
        self.cancel_requested.set()
        self.log("DOWNLOAD_CANCELLATION_REQUESTED")
        for future in self.futures:
            future.cancel()

    def _sleep_cancellable(self, seconds):
        """Sleep in short slices; returns False if the download was cancelled."""
        deadline = time.monotonic() + max(0.0, seconds)
        while not self.cancel_requested.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(0.2, remaining))
        return False

    def _run_batch_item(self, worker_fn, item):
        """
        Run one batch item. Workers handle their own download errors; this
        catches anything unexpected so one item cannot abort the batch.
        """
        try:
            worker_fn(item)
        except Exception as e:
            label = item.get("media_url") if isinstance(item, dict) else item
            self.log("UNEXPECTED_DOWNLOAD_ERROR", item=label, error=e)
            with self.file_lock:
                self.failed_files.append(str(label))
            self._emit_global_progress()

    def wait_for_futures(self, futures, cancel_log_key=None):
        for future in as_completed(futures):
            if self.cancel_requested.is_set():
                if cancel_log_key:
                    self.log(cancel_log_key)
                break
            try:
                future.result()
            except Exception as e:
                self.log("UNEXPECTED_DOWNLOAD_ERROR", item=self.domain_name, error=e)

    def run_batch(self, items, worker_fn, cancel_log_key=None):
        self.total_files = len(items)
        self.completed_files = 0
        self.futures = [self.executor.submit(self._run_batch_item, worker_fn, item) for item in items]
        self.wait_for_futures(self.futures, cancel_log_key)

    def shutdown_executor(self):
        if not self.shutdown_called:
            self.shutdown_called = True
            if self.executor:
                self.executor.shutdown(wait=True)
            if self.enable_widgets_callback:
                self.enable_widgets_callback()
            self.log("ALL_DOWNLOADS_COMPLETED_OR_CANCELLED")
            if self.duplicate_files:
                self.log("DUPLICATES_REMOVED_SUMMARY", count=len(self.duplicate_files))
            if self.failed_files:
                self.log("DOWNLOAD_SUMMARY_FAILED", failed=len(self.failed_files))
            with self.db_lock:
                try:
                    self.db_connection.close()
                except Exception:
                    pass

    def _filename_from_url(self, media_url):
        """
        Some hosts serve the file under a hash or a UUID and send the real
        name in the query string, so the path alone gives useless names.
        """
        parsed = urlparse(media_url)
        path_name = os.path.basename(parsed.path)

        query = dict(parse_qsl(parsed.query))
        for key in self.filename_query_keys:
            # parse_qsl already decodes the value.
            candidate = query.get(key) or ""
            candidate = os.path.basename(candidate.replace("\\", "/")).strip()

            if not candidate or candidate in (".", ".."):
                continue

            if not os.path.splitext(candidate)[1]:
                candidate += os.path.splitext(path_name)[1]

            return candidate

        return path_name

    def get_filename(self, media_url, post_id=None, post_name=None, attachment_index=1, post_time=None,
                     user=None, forced_name=None):
        base_name = forced_name or self._filename_from_url(media_url)
        name_no_ext, extension = os.path.splitext(base_name)

        mode = self.file_naming_mode

        def sanitize(name):
            return self.sanitize_filename(name).strip()

        def title_part():
            title = sanitize(post_name or "")
            if title:
                return self._fit_bytes(title, MAX_TITLE_BYTES, keep_ext=False)
            return f"post_{post_id}" if post_id else "post"

        if mode == CUSTOM_TEMPLATE_MODE:
            fields = {
                "name": name_no_ext,
                "ext": extension,
                "index": attachment_index,
                "title": post_name or "",
                "post_id": post_id or "",
                "published": post_time or "",
                "date": date_from_published(post_time),
                "user": user or "",
                "site": getattr(self, "domain_name", "") or "",
                "type": self._media_type_folder(extension.lower()),
                "url_hash": f"{zlib.crc32(media_url.encode('utf-8')) & 0xFFFF:04x}",
            }
            try:
                return render_template(self.file_name_template or DEFAULT_TEMPLATE, fields)
            except TemplateError as e:
                if not self._template_error_logged:
                    self._template_error_logged = True
                    self.log("FILENAME_TEMPLATE_INVALID", error=e)
                mode = 0

        if mode == 0:
            sanitized = sanitize(name_no_ext) or "file"
            return f"{sanitized}_{attachment_index}{extension}"
        elif mode == 1:
            sanitized_post = title_part()
            short_hash = f"{zlib.crc32(media_url.encode('utf-8')) & 0xFFFF:04x}"
            return f"{sanitized_post}_{attachment_index}_{short_hash}{extension}"
        elif mode == 2:
            sanitized_post = title_part()
            return f"{sanitized_post} - {post_id}_{attachment_index}{extension}" if post_id else f"{sanitized_post}_{attachment_index}{extension}"
        elif mode == 3:
            sanitized_post = title_part()
            sanitized_time = sanitize(post_time or "")
            short_hash = f"{zlib.crc32(media_url.encode('utf-8')) & 0xFFFF:04x}"
            return f"{sanitized_time} - {sanitized_post}_{attachment_index}_{short_hash}{extension}"

        return sanitize(name_no_ext) + extension

    def _media_type_folder(self, extension):
        if extension in self.video_extensions:
            return "videos"
        elif extension in self.image_extensions:
            return "images"
        elif extension in self.document_extensions:
            return "documents"
        elif extension in self.compressed_extensions:
            return "compressed"
        return "other"

    def get_media_folder(self, extension, user_id, post_id=None):
        folder_name = self._media_type_folder(extension)

        if self.folder_structure == "post_number" and post_id:
            return os.path.join(self.download_folder, user_id, f"post_{post_id}", folder_name)

        return os.path.join(self.download_folder, user_id, folder_name)

    # ------------------------------------------------------------------
    # Hashing and duplicate detection
    # ------------------------------------------------------------------
    @staticmethod
    def _path_key(path):
        return os.path.normcase(os.path.abspath(path))

    def _model_root(self, media_folder):
        """The top-level folder of the model (user, album or thread) a file
        belongs to; duplicates are only looked for inside it."""
        base = os.path.abspath(self.download_folder or ".")
        folder = os.path.abspath(media_folder)
        try:
            rel = os.path.relpath(folder, base)
        except ValueError:
            return folder
        if rel == "." or rel == ".." or rel.startswith(".." + os.sep):
            return folder
        return os.path.join(base, rel.split(os.sep)[0])

    def _hash_file(self, path):
        hasher = new_hasher()
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(HASH_CHUNK_SIZE), b""):
                    hasher.update(chunk)
        except OSError:
            return None
        return hasher.hexdigest()

    def _backfill_model_hashes(self, model_root):
        """Hash files recorded before hashing existed (or with an older hash
        algorithm), once per model, so new downloads can be compared against them."""
        root_key = self._path_key(model_root)
        with self._backfill_lock:
            if root_key in self._hash_backfill_done:
                return

            with self.db_lock:
                self.db_cursor.execute(
                    "SELECT id, file_path FROM downloads "
                    "WHERE (file_hash IS NULL OR length(file_hash) != ?) "
                    "AND file_path IS NOT NULL",
                    (HASH_HEX_LENGTH,),
                )
                rows = self.db_cursor.fetchall()

            pending = [
                (row_id, path) for row_id, path in rows
                if self._path_key(path).startswith(root_key + os.sep) and os.path.isfile(path)
            ]
            if pending:
                self.log("HASHING_EXISTING_FILES", count=len(pending), folder=model_root)

            for row_id, path in pending:
                if self.cancel_requested.is_set():
                    return
                file_hash = self._hash_file(path)
                if file_hash:
                    with self.db_lock:
                        self.db_cursor.execute(
                            "UPDATE downloads SET file_hash = ? WHERE id = ?",
                            (file_hash, row_id),
                        )
                        self.db_connection.commit()

            self._hash_backfill_done.add(root_key)

    def _find_duplicate(self, file_hash, model_root, exclude_path=None):
        """Path of an existing file in the same model with this hash.
        Caller must hold db_lock."""
        root_key = self._path_key(model_root)
        exclude_key = self._path_key(exclude_path) if exclude_path else None
        self.db_cursor.execute(
            "SELECT file_path FROM downloads WHERE file_hash = ?", (file_hash,)
        )
        for (path,) in self.db_cursor.fetchall():
            if not path:
                continue
            key = self._path_key(path)
            if key == exclude_key or not key.startswith(root_key + os.sep):
                continue
            if os.path.isfile(path):
                return path
        return None

    @staticmethod
    def _unique_path(path):
        stem, ext = os.path.splitext(path)
        n = 2
        while os.path.exists(f"{stem} ({n}){ext}"):
            n += 1
        return f"{stem} ({n}){ext}"

    def _commit_download(self, tmp_path, final_path, file_hash, model_root, record_url, size, user_id, post_id):
        """Move a finished download into place and record it in the database.

        Returns (path_on_disk, is_duplicate). Only called once the file is
        complete, so the database never holds partial downloads.
        """
        if self.dedupe_by_hash:
            self._backfill_model_hashes(model_root)

        # Hash a same-named file outside the locks; this is the slow part.
        existing_hash = self._hash_file(final_path) if os.path.exists(final_path) else None

        with self.db_lock:
            duplicate_of = None
            if self.dedupe_by_hash:
                duplicate_of = self._find_duplicate(file_hash, model_root, exclude_path=final_path)

            with self.file_lock:
                if duplicate_of is None and existing_hash == file_hash and os.path.exists(final_path):
                    duplicate_of = final_path

                if duplicate_of is not None:
                    os.remove(tmp_path)
                    path = duplicate_of
                else:
                    # Never overwrite a different file that took this name.
                    path = self._unique_path(final_path) if os.path.exists(final_path) else final_path
                    os.replace(tmp_path, path)
                self.completed_files += 1

            self.db_cursor.execute(
                """
                INSERT OR REPLACE INTO downloads (media_url, file_path, file_size, user_id, post_id, file_hash)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (record_url, path, size, user_id, post_id, file_hash),
            )
            self.db_connection.commit()
            self.download_cache[record_url] = (path, size)

        return path, duplicate_of is not None

    def _compute_retry_delay(self, attempt_index, response=None):
        if response is not None:
            raw = response.headers.get("Retry-After")
            if raw:
                try:
                    retry_after = float(raw)
                except (TypeError, ValueError):
                    pass
                else:
                    if 0 <= retry_after <= 60:
                        return retry_after

        base = max(float(self.retry_interval or 0), 0.1)
        return (base * (attempt_index + 1)) + random.uniform(0.35, 1.15)

    def _wait_for_domain_cooldown(self, domain):
        while True:
            if self.cancel_requested.is_set():
                return False

            with self.domain_error_lock:
                cooldown_until = self.domain_error_state[domain]["cooldown_until"]

            remaining = cooldown_until - time.time()
            if remaining <= 0:
                return True

            time.sleep(min(remaining, 0.5))

    def _mark_domain_success(self, domain):
        with self.domain_error_lock:
            state = self.domain_error_state[domain]
            state["burst_count"] = 0
            state["last_error_ts"] = 0.0
            state["cooldown_until"] = 0.0

    def _mark_domain_error(self, domain, status_code):
        if status_code not in (429, 500, 502, 503, 504):
            return

        now = time.time()
        with self.domain_error_lock:
            state = self.domain_error_state[domain]

            if now - state["last_error_ts"] > self.domain_error_window:
                state["burst_count"] = 0

            state["burst_count"] += 1
            state["last_error_ts"] = now

            if state["burst_count"] >= self.domain_error_threshold:
                state["cooldown_until"] = max(
                    state["cooldown_until"],
                    now + self.domain_cooldown_seconds,
                )

    def safe_request(self, url, max_retries=None, headers=None):
        if max_retries is None:
            max_retries = self.max_retries
        if headers is None:
            headers = self.headers

        domain = urlparse(url).netloc

        for attempt in range(max_retries + 1):
            if self.cancel_requested.is_set():
                return None

            if not self._wait_for_domain_cooldown(domain):
                return None

            with self.domain_locks[domain]:
                elapsed_time = time.time() - self.domain_last_request[domain]
                if elapsed_time < self.rate_limit_interval:
                    time.sleep(self.rate_limit_interval - elapsed_time)

                try:
                    self.domain_last_request[domain] = time.time()
                    response = self.session.get(url, stream=True, headers=headers, timeout=self.request_timeout)
                    response.raise_for_status()
                    self._mark_domain_success(domain)
                    return response
                except requests.exceptions.RequestException as e:
                    error = e

            # The failure is handled outside the per-domain slot so other
            # downloads from this host keep going during the backoff.
            error_response = getattr(error, "response", None)
            status_code = getattr(error_response, "status_code", None)
            is_last_attempt = attempt == max_retries

            if status_code in (429, 500, 502, 503, 504):
                self._mark_domain_error(domain, status_code)

            # A 403/404 means the URL itself is wrong or expired, so
            # repeating the same request cannot help.
            give_up = is_last_attempt or status_code in (403, 404)
            retry_delay = None if give_up else self._compute_retry_delay(attempt, error_response)
            if error_response is not None:
                error_response.close()

            if give_up:
                self.log(
                    "FINAL_FAILURE_ACCESSING_URL",
                    url=url,
                    status_code=status_code if status_code is not None else error,
                )
                return None

            if status_code in (429, 500, 502, 503, 504):
                self.log(
                    "HTTP_RETRY_REQUEST",
                    attempt=attempt + 1,
                    total=max_retries + 1,
                    status_code=status_code,
                    url=url,
                    delay=round(retry_delay, 1),
                )
            elif isinstance(error, requests.exceptions.ReadTimeout):
                self.log(
                    "READ_TIMEOUT_RETRY",
                    attempt=attempt + 1,
                    total=max_retries + 1,
                    timeout=self.request_timeout[1],
                )
            else:
                url_display = getattr(error.request, "url", None) or url
                if len(url_display) > 60:
                    url_display = url_display[:60] + "..."
                self.log(
                    "ERROR_ACCESSING_URL",
                    attempt=attempt + 1,
                    total=max_retries + 1,
                    url=url_display,
                    error=error,
                )

            if not self._sleep_cancellable(retry_delay):
                return None

        return None

    def _emit_global_progress(self):
        if self.update_global_progress_callback:
            with self.file_lock:
                # Failed files are finished too; leaving them out kept the
                # bar below 100% whenever anything failed.
                done = self.completed_files + len(self.skipped_files) + len(self.failed_files)
            self.update_global_progress_callback(done, self.total_files)

    def _report_progress(self, downloaded, total, start_time, download_id, tmp_path):
        if not self.update_progress_callback:
            return
        elapsed_time = time.time() - start_time
        speed = downloaded / elapsed_time if elapsed_time > 0 else 0
        remaining_time = (total - downloaded) / speed if speed > 0 and total > 0 else 0
        self.update_progress_callback(
            downloaded,
            total,
            file_id=download_id,
            file_path=tmp_path,
            speed=speed,
            eta=remaining_time,
        )

    def _finish_progress(self, download_id, tmp_path):
        """Tell the UI this file is done, whether it succeeded or not."""
        if self.update_progress_callback and download_id is not None:
            self.update_progress_callback(0, 0, file_id=download_id, file_path=tmp_path, finished=True)

    def _claim_tmp_path(self, final_path, tmp_path):
        """
        Register tmp_path as in use and delete temporary files for the same
        target left behind by a run that was killed mid-download. Runs under
        the lock so a temporary file another thread is about to create is
        never mistaken for a leftover.
        """
        with BaseApiDownloader._active_tmp_lock:
            BaseApiDownloader._active_tmp_paths.add(tmp_path)
            for stale in glob.glob(glob.escape(final_path) + ".*.tmp"):
                if stale in BaseApiDownloader._active_tmp_paths:
                    continue
                if not _TMP_SUFFIX_RE.fullmatch(stale[len(final_path):]):
                    continue
                try:
                    os.remove(stale)
                except OSError:
                    pass

    def _release_tmp_path(self, tmp_path):
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        with BaseApiDownloader._active_tmp_lock:
            BaseApiDownloader._active_tmp_paths.discard(tmp_path)

    @staticmethod
    def _content_length(response):
        """Size of the body as written to disk, or 0 when it is unknown."""
        encoding = (response.headers.get("Content-Encoding") or "identity").strip().lower()
        if encoding != "identity":
            # The header counts compressed bytes; iter_content yields
            # decoded ones, so the two can never be compared.
            return 0
        try:
            return max(0, int(response.headers.get("Content-Length") or 0))
        except ValueError:
            return 0

    @staticmethod
    def _range_start(response):
        match = re.match(r"\s*bytes\s+(\d+)-", response.headers.get("Content-Range") or "")
        return int(match.group(1)) if match else None

    @staticmethod
    def _stream_error_text(error):
        """'Connection broken: IncompleteRead(...)' instead of the nested
        tuple that ChunkedEncodingError prints."""
        inner = error.args[0] if error.args else None
        if isinstance(inner, Exception) and inner.args and isinstance(inner.args[0], str):
            return inner.args[0]
        return str(error)

    def _write_stream(self, response, tmp_path, mode, hasher, downloaded, total_size, start_time, download_id):
        """
        Append the response body to tmp_path. Returns (downloaded, error):
        a dropped connection is returned instead of raised so the caller
        knows how many bytes made it to disk and can resume from there.
        """
        try:
            with open(tmp_path, mode) as f:
                for chunk in response.iter_content(chunk_size=1048576):
                    if self.cancel_requested.is_set():
                        raise DownloadCancelled()
                    if not chunk:
                        continue
                    f.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    self._report_progress(downloaded, total_size, start_time, download_id, tmp_path)
        except requests.exceptions.RequestException as e:
            return downloaded, e
        finally:
            response.close()
        return downloaded, None

    def _request_or_fail(self, url, headers, failure_key, **kwargs):
        response = self.safe_request(url, max_retries=self.max_retries, headers=headers)
        if response is None:
            if self.cancel_requested.is_set():
                raise DownloadCancelled()
            raise DownloadError(self._translate_text(failure_key, **kwargs))
        return response

    def _download_to_tmp(self, media_url, request_headers, fallback_url, tmp_path, download_id):
        """
        Download media_url into tmp_path, resuming with Range requests after
        dropped connections. Returns (size, hash).
        """
        request_url = media_url
        response = self.safe_request(request_url, max_retries=self.max_retries, headers=request_headers)

        if response is None and fallback_url and not self.cancel_requested.is_set():
            self.log("PAWCHIVE_PREVIEW_FALLBACK", media_url=media_url)
            request_url = fallback_url
            response = self.safe_request(request_url, max_retries=self.max_retries, headers=request_headers)

        if response is None:
            if self.cancel_requested.is_set():
                raise DownloadCancelled()
            raise DownloadError(self._translate_text("DOWNLOAD_NO_RESPONSE", total=self.max_retries + 1))

        total_size = self._content_length(response)
        downloaded = 0
        hasher = new_hasher()
        start_time = time.time()
        # Rounds in a row that added no bytes (known size) or failed
        # restarts (unknown size); a round that makes progress resets it.
        failed_rounds = 0

        while True:
            before = downloaded
            downloaded, error = self._write_stream(
                response, tmp_path, "ab" if downloaded else "wb",
                hasher, downloaded, total_size, start_time, download_id,
            )

            if total_size and downloaded >= total_size:
                break
            if not total_size and error is None:
                break

            if error is not None:
                error = self._stream_error_text(error)
                self.log("DOWNLOAD_INTERRUPTED", media_url=media_url, downloaded_size=downloaded, error=error)

            if total_size and downloaded > before:
                failed_rounds = 0
            else:
                failed_rounds += 1
            if failed_rounds > self.max_retries:
                raise DownloadError(error or self._translate_text("RESUME_NO_PROGRESS"))

            if error is not None and not self._sleep_cancellable(self._compute_retry_delay(failed_rounds)):
                raise DownloadCancelled()

            if total_size:
                self.log("RESUMING_DOWNLOAD_AT_BYTE", downloaded_size=downloaded, media_url=media_url)
                response = self._request_or_fail(
                    request_url,
                    {**request_headers, "Range": f"bytes={downloaded}-"},
                    "RESUMPTION_FAILED_AFTER_RETRIES",
                )
                if response.status_code == 206 and self._range_start(response) == downloaded:
                    continue
                if response.status_code == 206:
                    # A partial body from the wrong offset cannot be used.
                    response.close()
                    response = self._request_or_fail(request_url, request_headers, "RESUMPTION_FAILED_AFTER_RETRIES")
            else:
                # Without a known size there is no safe offset to resume from.
                response = self._request_or_fail(
                    request_url, request_headers, "DOWNLOAD_NO_RESPONSE", total=self.max_retries + 1
                )

            self.log("RESTARTING_DOWNLOAD", media_url=media_url)
            downloaded = 0
            hasher = new_hasher()
            total_size = self._content_length(response) or total_size

        if total_size > 0 and downloaded != total_size:
            raise DownloadError(
                self._translate_text("FINAL_SIZE_MISMATCH", expected=total_size, actual=downloaded)
            )

        return downloaded, hasher.hexdigest()

    def process_media_element(
        self,
        media_url,
        user_id=None,
        post_id=None,
        post_name=None,
        post_time=None,
        download_id=None,
        target_folder=None,
        forced_filename=None,
        headers=None,
        record_url=None,
        fallback_url=None,
    ):
        if self.cancel_requested.is_set():
            return

        request_headers = {**self.headers, **headers} if headers else self.headers
        record_url = record_url or media_url

        extension = os.path.splitext(self._filename_from_url(media_url))[1].lower()

        if (extension in self.image_extensions and not self.download_images) or \
                (extension in self.video_extensions and not self.download_videos) or \
                (extension in self.compressed_extensions and not self.download_compressed):
            self.log("SKIPPING_MEDIA_DUE_TO_SETTINGS", media_url=media_url)
            with self.file_lock:
                self.skipped_files.append(media_url)
            self._emit_global_progress()
            return

        counter_key = post_id or target_folder or user_id or "default"
        with self.counter_lock:
            self.post_attachment_counter[counter_key] += 1
            attachment_index = self.post_attachment_counter[counter_key]

        if target_folder:
            media_folder = target_folder
        else:
            effective_user_id = user_id or "generic"
            media_folder = self.get_media_folder(extension, effective_user_id, post_id)

        model_root = self._model_root(media_folder)

        if forced_filename and self.file_naming_mode != CUSTOM_TEMPLATE_MODE:
            # Forced names can come straight from a file host's API, so
            # any folder part is dropped and the rest sanitized.
            filename = self._safe_filename(forced_filename)
        else:
            filename = self._safe_relative_path(self.get_filename(
                media_url,
                post_id=post_id,
                post_name=post_name,
                post_time=post_time,
                attachment_index=attachment_index,
                user=user_id or os.path.basename(model_root),
                forced_name=forced_filename,
            ))

        final_path = os.path.normpath(os.path.join(media_folder, filename))
        tmp_path = f"{final_path}.{threading.get_ident()}.tmp"

        with self.db_lock:
            already_downloaded = self.skip_downloaded and record_url in self.download_cache

        if already_downloaded:
            self.log("FILE_ALREADY_IN_DB_SKIPPING", media_url=record_url)
            with self.file_lock:
                self.skipped_files.append(final_path)
            self._emit_global_progress()
            return

        try:
            os.makedirs(os.path.dirname(final_path), exist_ok=True)
            self._claim_tmp_path(final_path, tmp_path)
            self.log("STARTING_DOWNLOAD_FROM", media_url=media_url)

            downloaded_size, file_hash = self._download_to_tmp(
                media_url, request_headers, fallback_url, tmp_path, download_id
            )

            saved_path, is_duplicate = self._commit_download(
                tmp_path,
                final_path,
                file_hash,
                model_root,
                record_url,
                downloaded_size,
                user_id,
                post_id,
            )

            if is_duplicate:
                with self.file_lock:
                    self.duplicate_files.append(media_url)
                self.log("DUPLICATE_FILE_REMOVED", media_url=media_url, existing=saved_path)
            else:
                self.log("DOWNLOAD_SUCCESS_FROM", media_url=media_url)
            self._emit_global_progress()

        except DownloadCancelled:
            self.log("DOWNLOAD_CANCELLED_FROM", media_url=media_url)
        except Exception as e:
            if self.cancel_requested.is_set():
                self.log("DOWNLOAD_CANCELLED_FROM", media_url=media_url)
                return
            self.log("DOWNLOAD_FAILED_WITH_ERROR", media_url=media_url, error=e)
            with self.file_lock:
                self.failed_files.append(media_url)
            self._emit_global_progress()
        finally:
            self._release_tmp_path(tmp_path)
            self._finish_progress(download_id, tmp_path)


    def update_max_downloads(self, new_max):
        try:
            new_max = int(new_max)
        except (TypeError, ValueError):
            return

        if new_max < 1:
            new_max = 1

        self.max_workers = new_max

        # wait=False: this runs on the UI thread when settings are applied
        # mid-download; waiting would freeze the interface until every
        # queued file finishes. In-flight tasks drain on the old executor.
        if self.executor:
            self.executor.shutdown(wait=False)

        self.executor = ThreadPoolExecutor(max_workers=new_max)
        self.domain_locks = defaultdict(lambda: Semaphore(self.per_domain_limit))

        self.log(
            "UPDATED_MAX_WORKERS",
            new_max=new_max,
            per_domain_limit=self.per_domain_limit,
        )
