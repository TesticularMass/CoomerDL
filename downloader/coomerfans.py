from concurrent.futures import as_completed

from downloader.core.base_api_downloader import BaseApiDownloader
from downloader.adapters.coomerfans_adapter import CoomerfansAdapter
from downloader.adapters.http_retry import ScrapeCancelled


class CoomerfansDownloader(BaseApiDownloader):
    def __init__(self, is_profile_download=False, **kwargs):
        kwargs.setdefault("download_compressed", False)
        super().__init__(**kwargs)
        self.is_profile_download = is_profile_download
        self.adapter = CoomerfansAdapter(
            session=self.session,
            headers=self.headers,
            log_callback=self.log_callback,
            tr=self.tr,
            should_cancel=self.cancel_requested.is_set,
            max_retries=self.max_retries,
            retry_interval=self.retry_interval,
            request_interval=self.rate_limit_interval,
            cache_db_path=self.db_path,
        )
        self.domain_name = "coomerfans"

    def request_cancel(self):
        super().request_cancel()
        self.log("COOMERFANS_DOWNLOAD_CANCELLED")

    def _submit_entry(self, entry, default_user_id=None):
        media_url = entry["media_url"]
        return self.executor.submit(
            self.process_media_element,
            media_url,
            user_id=entry.get("user_id") or default_user_id or "coomerfans",
            post_id=entry.get("post_id"),
            post_name=entry.get("title"),
            post_time=entry.get("published"),
            download_id=media_url,
            record_url=entry.get("record_url"),
        )

    def process_post_page(self, page_url, base_folder, download_images=True, download_videos=True):
        try:
            if self.cancel_requested.is_set():
                return
            if base_folder:
                self.download_folder = base_folder
            self.log("COOMERFANS_PROCESSING_POST_URL", page_url=page_url)
            resolved = self.adapter._resolve_post(
                page_url,
                download_images=download_images,
                download_videos=download_videos,
            )
            default_user_id = resolved.get("folder_name")
            self.run_batch(
                resolved["media"],
                lambda entry: self.process_media_element(
                    entry["media_url"],
                    user_id=entry.get("user_id") or default_user_id or "coomerfans",
                    post_id=entry.get("post_id"),
                    post_name=entry.get("title"),
                    post_time=entry.get("published"),
                    download_id=entry["media_url"],
                    record_url=entry.get("record_url"),
                ),
                "COOMERFANS_CANCELLING_REMAINING_DOWNLOADS",
            )
            self.log("COOMERFANS_POST_DOWNLOAD_COMPLETE", folder_name=resolved["folder_name"])
        except ScrapeCancelled:
            self.log("COOMERFANS_DOWNLOAD_CANCELLED")
        except Exception as e:
            self.log("COOMERFANS_ERROR_ACCESSING_PAGE", page_url=page_url, status_code=str(e))
        finally:
            self.shutdown_executor()

    def process_profile_page(self, url, download_folder, download_images, download_videos, only_first_page=False):
        try:
            if self.cancel_requested.is_set():
                return
            if download_folder:
                self.download_folder = download_folder
            self.log("COOMERFANS_PROCESSING_PROFILE_URL", url=url)
            self.total_files = 0
            self.completed_files = 0
            self.futures = []

            def enqueue_post_media(items, default_user_id):
                if self.cancel_requested.is_set():
                    return
                with self.counter_lock:
                    self.total_files += len(items)
                self._emit_global_progress()
                for entry in items:
                    if self.cancel_requested.is_set():
                        break
                    self.futures.append(self._submit_entry(entry, default_user_id))

            resolved = self.adapter._resolve_profile(
                url,
                download_images=download_images,
                download_videos=download_videos,
                only_first_page=only_first_page,
                on_media=enqueue_post_media,
            )

            for future in as_completed(self.futures):
                if self.cancel_requested.is_set():
                    self.log("COOMERFANS_CANCELLING_REMAINING_DOWNLOADS")
                    break
                future.result()

            self.log("COOMERFANS_PROFILE_DOWNLOAD_COMPLETE", username=resolved["folder_name"])
        except ScrapeCancelled:
            self.log("COOMERFANS_DOWNLOAD_CANCELLED")
        except Exception as e:
            self.log("COOMERFANS_ERROR_ACCESSING_PAGE", page_url=url, status_code=str(e))
        finally:
            self.shutdown_executor()
