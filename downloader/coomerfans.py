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
        )
        self.domain_name = "coomerfans"

    def request_cancel(self):
        self.cancel_requested.set()
        self.log("COOMERFANS_DOWNLOAD_CANCELLED")
        if self.is_profile_download and self.enable_widgets_callback:
            self.enable_widgets_callback()

    def _run_page(self, url, resolve_fn, start_log, start_kw, done_log, done_kw, download_images, download_videos):
        try:
            if self.cancel_requested.is_set():
                return
            self.log(start_log, **{start_kw: url})
            resolved = resolve_fn(url, download_images=download_images, download_videos=download_videos)
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
                ),
                "COOMERFANS_CANCELLING_REMAINING_DOWNLOADS",
            )
            self.log(done_log, **{done_kw: resolved["folder_name"]})
        except ScrapeCancelled:
            self.log("COOMERFANS_DOWNLOAD_CANCELLED")
        except Exception as e:
            self.log("COOMERFANS_ERROR_ACCESSING_PAGE", page_url=url, status_code=str(e))
        finally:
            self.shutdown_executor()

    def process_post_page(self, page_url, base_folder, download_images=True, download_videos=True):
        self._run_page(page_url, self.adapter._resolve_post, "COOMERFANS_PROCESSING_POST_URL", "page_url", "COOMERFANS_POST_DOWNLOAD_COMPLETE", "folder_name", download_images, download_videos)

    def process_profile_page(self, url, download_folder, download_images, download_videos):
        self._run_page(url, self.adapter._resolve_profile, "COOMERFANS_PROCESSING_PROFILE_URL", "url", "COOMERFANS_PROFILE_DOWNLOAD_COMPLETE", "username", download_images, download_videos)
