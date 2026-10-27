import os

from downloader.core.base_api_downloader import BaseApiDownloader
from downloader.adapters.erome_adapter import EromeAdapter


class EromeDownloader(BaseApiDownloader):
    def __init__(self, is_profile_download=False, **kwargs):
        kwargs.setdefault("download_compressed", False)
        super().__init__(**kwargs)
        self.is_profile_download = is_profile_download
        self.adapter = EromeAdapter(
            session=self.session,
            headers=self.headers,
            log_callback=self.log_callback,
            tr=self.tr,
            should_cancel=self.cancel_requested.is_set,
            max_retries=self.max_retries,
            retry_interval=self.retry_interval,
            cache_db_path=self.db_path,
        )
        self.domain_name = "erome"

    def request_cancel(self):
        super().request_cancel()
        self.log("EROME_DOWNLOAD_CANCELLED")

    def _download_entry(self, entry, root_folder):
        folder_name = entry.get("folder_name") or "erome_album"
        target_folder = os.path.join(root_folder, folder_name) if not os.path.isabs(folder_name) else folder_name
        # process_media_element creates the folder inside its own error
        # handling, so a bad folder name fails one file, not the batch.
        self.process_media_element(
            entry["media_url"],
            post_id=entry.get("post_id"),
            post_name=entry.get("title"),
            post_time=entry.get("published"),
            download_id=entry["media_url"],
            target_folder=target_folder,
            forced_filename=entry.get("filename"),
        )

    def _run_page(self, url, root_folder, resolve_fn, start_log, start_kw, done_log, done_kw, download_images,
                  download_videos, **resolve_kwargs):
        try:
            if self.cancel_requested.is_set():
                return
            self.log(start_log, **{start_kw: url})
            resolved = resolve_fn(url, download_images=download_images, download_videos=download_videos,
                                  **resolve_kwargs)
            if self.adapter.scrape_incomplete:
                self.incomplete_reason = self._translate_text("PROFILE_LISTING_INCOMPLETE")
            self.run_batch(
                resolved["media"],
                lambda entry: self._download_entry(entry, root_folder),
                "EROME_CANCELLING_REMAINING_DOWNLOADS",
            )
            self.log(done_log, **{done_kw: resolved["folder_name"]})
        except Exception as e:
            self.log("EROME_ERROR_ACCESSING_PAGE", page_url=url, status_code=str(e))
        finally:
            self.shutdown_executor()

    def process_album_page(self, page_url, base_folder, download_images=True, download_videos=True):
        self._run_page(page_url, base_folder, self.adapter._resolve_album, "EROME_PROCESSING_ALBUM_URL", "page_url", "EROME_ALBUM_DOWNLOAD_COMPLETE", "folder_name", download_images, download_videos)

    def process_profile_page(self, url, download_folder, download_images, download_videos, only_first_page=False):
        self._run_page(url, download_folder, self.adapter._resolve_profile, "EROME_PROCESSING_PROFILE_URL", "url", "EROME_PROFILE_DOWNLOAD_COMPLETE", "username", download_images, download_videos, only_first_page=only_first_page)
