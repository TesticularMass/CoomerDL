from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Semaphore
from urllib.parse import parse_qsl, urlparse
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


class BaseApiDownloader:
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
        return re.sub(r'[<>:"/\\\\|?*]', "_", filename)

    def request_cancel(self):
        self.cancel_requested.set()
        self.log("DOWNLOAD_CANCELLATION_REQUESTED")
        for future in self.futures:
            future.cancel()

    def run_batch(self, items, worker_fn, cancel_log_key=None):
        self.total_files = len(items)
        self.completed_files = 0
        self.futures = [self.executor.submit(worker_fn, item) for item in items]
        for future in as_completed(self.futures):
            if self.cancel_requested.is_set():
                if cancel_log_key:
                    self.log(cancel_log_key)
                break
            future.result()

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
            sanitized_post = sanitize(post_name or "") or (f"post_{post_id}" if post_id else "post")
            short_hash = f"{zlib.crc32(media_url.encode('utf-8')) & 0xFFFF:04x}"
            return f"{sanitized_post}_{attachment_index}_{short_hash}{extension}"
        elif mode == 2:
            sanitized_post = sanitize(post_name or "") or (f"post_{post_id}" if post_id else "post")
            return f"{sanitized_post} - {post_id}_{attachment_index}{extension}" if post_id else f"{sanitized_post}_{attachment_index}{extension}"
        elif mode == 3:
            sanitized_post = sanitize(post_name or "") or (f"post_{post_id}" if post_id else "post")
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

        parsed = urlparse(url)
        domain = parsed.netloc
        path = parsed.path

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
                    sc = response.status_code

                    response.raise_for_status()
                    self._mark_domain_success(domain)
                    return response

                except requests.exceptions.RequestException as e:
                    error_response = getattr(e, "response", None)
                    status_code = getattr(error_response, "status_code", None)
                    is_last_attempt = attempt == max_retries

                    if status_code in (429, 500, 502, 503, 504):
                        self._mark_domain_error(domain, status_code)

                    # A 403/404 means the URL itself is wrong or expired, so
                    # repeating the same request cannot help. Coomer/kemono
                    give_up = is_last_attempt or status_code in (403, 404)

                    if give_up:
                        # These used to exhaust every retry in silence, which
                        # left failures with no explanation in the log.
                        self.log(
                            "FINAL_FAILURE_ACCESSING_URL",
                            url=url,
                            status_code=status_code if status_code is not None else e,
                        )
                        return None

                    retry_delay = self._compute_retry_delay(attempt, error_response)

                    if status_code in (429, 500, 502, 503, 504):
                        self.log(
                            "HTTP_RETRY_REQUEST",
                            attempt=attempt + 1,
                            total=max_retries + 1,
                            status_code=status_code,
                            url=url,
                            delay=round(retry_delay, 1),
                        )
                    elif isinstance(e, requests.exceptions.ReadTimeout):
                        self.log(
                            "READ_TIMEOUT_RETRY",
                            attempt=attempt + 1,
                            total=max_retries + 1,
                            timeout=self.request_timeout[1],
                        )
                    else:
                        url_display = getattr(e.request, "url", url)
                        if len(url_display) > 60:
                            url_display = url_display[:60] + "..."
                        self.log(
                            "ERROR_ACCESSING_URL",
                            attempt=attempt + 1,
                            total=max_retries + 1,
                            url=url_display,
                            error=e,
                        )

                    time.sleep(retry_delay)

        return None

    def _emit_global_progress(self):
        if self.update_global_progress_callback:
            with self.file_lock:
                done = self.completed_files + len(self.skipped_files)
            self.update_global_progress_callback(done, self.total_files)

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

        if (extension in self.image_extensions and not self.download_images) or         (extension in self.video_extensions and not self.download_videos) or         (extension in self.compressed_extensions and not self.download_compressed):
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
            filename = forced_filename
        else:
            filename = self.get_filename(
                media_url,
                post_id=post_id,
                post_name=post_name,
                post_time=post_time,
                attachment_index=attachment_index,
                user=user_id or os.path.basename(model_root),
                forced_name=forced_filename,
            )

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

        os.makedirs(os.path.dirname(final_path), exist_ok=True)

        self.log("STARTING_DOWNLOAD_FROM", media_url=media_url)

        request_url = media_url
        response = self.safe_request(request_url, max_retries=self.max_retries, headers=request_headers)

        if response is None and fallback_url:
            self.log("PAWCHIVE_PREVIEW_FALLBACK", media_url=media_url)
            request_url = fallback_url
            response = self.safe_request(request_url, max_retries=self.max_retries, headers=request_headers)

        if response is None:
            self.log(
                "FAILED_TO_DOWNLOAD_AFTER_ATTEMPTS",
                media_url=media_url,
                total=self.max_retries + 1,
            )
            with self.file_lock:
                self.failed_files.append(media_url)
            return

        try:
            try:
                total_size = int(response.headers.get("content-length", 0))
                downloaded_size = 0
                start_time = time.time()
                hasher = new_hasher()

                with open(tmp_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=1048576):
                        if self.cancel_requested.is_set():
                            raise Exception("CANCELLATION_REQUESTED")
                        if chunk:
                            f.write(chunk)
                            hasher.update(chunk)
                            downloaded_size += len(chunk)
                            if self.update_progress_callback:
                                elapsed_time = time.time() - start_time
                                speed = downloaded_size / elapsed_time if elapsed_time > 0 else 0
                                remaining_time = (total_size - downloaded_size) / speed if speed > 0 and total_size > 0 else 0
                                self.update_progress_callback(
                                    downloaded_size,
                                    total_size,
                                    file_id=download_id,
                                    file_path=tmp_path,
                                    speed=speed,
                                    eta=remaining_time,
                                )
            finally:
                response.close()

            zero_progress_rounds = 0
            while total_size and downloaded_size < total_size:
                resume_headers = dict(request_headers)
                resume_headers["Range"] = f"bytes={downloaded_size}-"
                self.log(
                    "RESUMING_DOWNLOAD_AT_BYTE",
                    downloaded_size=downloaded_size,
                    media_url=media_url,
                )

                part_response = self.safe_request(request_url, max_retries=self.max_retries, headers=resume_headers)
                if part_response is None:
                    raise Exception("RESUMPTION_FAILED_AFTER_RETRIES")

                try:
                    if part_response.status_code == 200:
                        downloaded_size = 0
                        open_mode = "wb"
                        hasher = new_hasher()
                    else:
                        open_mode = "ab"

                    bytes_before_round = downloaded_size

                    with open(tmp_path, open_mode) as f:
                        for chunk in part_response.iter_content(chunk_size=1048576):
                            if self.cancel_requested.is_set():
                                raise Exception("CANCELLATION_REQUESTED")
                            if chunk:
                                f.write(chunk)
                                hasher.update(chunk)
                                downloaded_size += len(chunk)
                                if self.update_progress_callback:
                                    elapsed_time = time.time() - start_time
                                    speed = downloaded_size / elapsed_time if elapsed_time > 0 else 0
                                    remaining_time = (total_size - downloaded_size) / speed if speed > 0 and total_size > 0 else 0
                                    self.update_progress_callback(
                                        downloaded_size,
                                        total_size,
                                        file_id=download_id,
                                        file_path=tmp_path,
                                        speed=speed,
                                        eta=remaining_time,
                                    )
                finally:
                    part_response.close()

                if downloaded_size == bytes_before_round:
                    zero_progress_rounds += 1
                    if zero_progress_rounds >= 3:
                        raise Exception("RESUME_NO_PROGRESS")
                else:
                    zero_progress_rounds = 0

            if total_size > 0 and downloaded_size != total_size:
                raise Exception(
                    self._translate_text(
                        "FINAL_SIZE_MISMATCH",
                        expected=total_size,
                        actual=downloaded_size,
                    )
                )

            saved_path, is_duplicate = self._commit_download(
                tmp_path,
                final_path,
                hasher.hexdigest(),
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

        except Exception as e:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

            if str(e) == "CANCELLATION_REQUESTED" or str(e) == self._translate_text("CANCELLATION_REQUESTED"):
                self.log("DOWNLOAD_CANCELLED_FROM", media_url=media_url)
                return

            self.log(
                "FAILED_TO_DOWNLOAD_AFTER_ATTEMPTS",
                media_url=media_url,
                total=self.max_retries + 1,
            )
            with self.file_lock:
                self.failed_files.append(media_url)

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
