import os

from downloader.core.base_api_downloader import BaseApiDownloader
from downloader.adapters.http_retry import ScrapeCancelled
from downloader.adapters.simpcity_adapter import SimpCityAdapter


class SimpCity(BaseApiDownloader):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.adapter = SimpCityAdapter(
            log_callback=self.log_callback,
            tr=self.tr,
            should_cancel=self.cancel_requested.is_set,
            max_retries=self.max_retries,
            retry_interval=self.retry_interval,
        )
        self.domain_name = "simpcity"

    def _download_entry(self, entry, target_folder):
        if self.cancel_requested.is_set():
            return
        try:
            files = self.adapter.resolve_embed(entry)
        except Exception as e:
            if self.cancel_requested.is_set():
                return
            self.log("SIMPCITY_EMBED_FAILED", url=entry["media_url"], error=e)
            with self.file_lock:
                self.failed_files.append(entry["media_url"])
            return

        if not files:
            self.log("SIMPCITY_EMBED_EMPTY", url=entry["media_url"])
            return

        if len(files) > 1:
            with self.counter_lock:
                self.total_files += len(files) - 1

        for item in files:
            self.process_media_element(
                item["media_url"],
                post_id=item.get("post_id"),
                post_name=item.get("title"),
                post_time=item.get("published"),
                download_id=item["media_url"],
                target_folder=target_folder,
                forced_filename=item.get("filename"),
                headers=item.get("headers"),
                record_url=item.get("record_url"),
            )

    def download_images_from_simpcity(self, url, paginate=True):
        try:
            self.log("SIMPCITY_PROCESSING_THREAD", url=url)
            resolved = self.adapter.resolve_thread(
                url,
                paginate=paginate,
                download_images=self.download_images,
                download_videos=self.download_videos,
                download_attachments=self.download_compressed,
            )
            target_folder = os.path.join(self.download_folder, resolved["folder_name"])
            os.makedirs(target_folder, exist_ok=True)
            self.run_batch(
                resolved["media"],
                lambda entry: self._download_entry(entry, target_folder),
                "SIMPCITY_DOWNLOAD_CANCELLED",
            )
            self.log("SIMPCITY_DOWNLOAD_COMPLETED")
        except ScrapeCancelled:
            self.log("SIMPCITY_DOWNLOAD_CANCELLED")
        except Exception as e:
            self.log("SIMPCITY_ERROR_PROCESSING_THREAD", error=e)
        finally:
            self.shutdown_executor()
