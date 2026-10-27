import os

from downloader.core.base_api_downloader import BaseApiDownloader
from downloader.adapters.bunkr_adapter import BunkrAdapter
from downloader.adapters.http_retry import ScrapeCancelled


class BunkrDownloader(BaseApiDownloader):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.adapter = BunkrAdapter(
            session=self.session,
            headers=self.headers,
            log_callback=self.log_callback,
            tr=self._translate_text,
            should_cancel=self.cancel_requested.is_set,
            max_retries=self.max_retries,
            retry_interval=self.retry_interval,
            request_interval=self.rate_limit_interval,
        )
        self.domain_name = "bunkr"

    def _descargar_url_bunkr(self, url, start_log_key, error_log_key):
        try:
            self.log(start_log_key, url=url)
            resolved = self.adapter.resolve_url(url)
            target_folder = os.path.join(self.download_folder, resolved["folder_name"])
            os.makedirs(target_folder, exist_ok=True)
            self.run_batch(
                resolved["media"],
                lambda entry: self.process_media_element(
                    entry["media_url"],
                    post_id=entry["post_id"],
                    post_name=entry["title"],
                    post_time=entry["published"],
                    download_id=entry["media_url"],
                    target_folder=target_folder,
                ),
            )
        except ScrapeCancelled:
            self.log("DOWNLOAD_CANCELLATION_REQUESTED")
        except Exception as e:
            self.log(error_log_key, url=url, error=e)
        finally:
            self.shutdown_executor()

    def descargar_post_bunkr(self, url_post):
        self._descargar_url_bunkr(url_post, "BUNKR_STARTING_POST_DOWNLOAD", "BUNKR_ERROR_PROCESSING_POST")

    def descargar_perfil_bunkr(self, url_perfil):
        self._descargar_url_bunkr(url_perfil, "BUNKR_STARTING_PROFILE_DOWNLOAD", "BUNKR_ERROR_PROCESSING_PROFILE")
