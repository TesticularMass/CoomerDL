import threading
from dataclasses import dataclass
from typing import Callable, Optional

from app.models.download_request import DownloadRequest
from app.version import USER_AGENT
from downloader.bunkr import BunkrDownloader
from downloader.coomerfans import CoomerfansDownloader
from downloader.downloader import Downloader
from downloader.erome import EromeDownloader
from downloader.simpcity import SimpCity
from app.models.profile_session import (
    ProfileQueueItem,
    ProfileSession,
    ProfileStatus,
)


DEAD_SITES = {
    "coomer.st": "coomerfans.com",
    "kemono.cr": "pawchive.pw",
    "jpg5.su": None,
}


@dataclass
class DownloadJob:
    downloader: object
    method: Callable
    args: tuple = ()


@dataclass
class JobResolutionError:
    # Translated message, usable as a per-profile failure reason.
    message: str
    # Whether the single-URL flow shows an error dialog for this failure
    # (the unknown-site case historically only logs).
    show_popup: bool = True


class MainController:
    def __init__(self, app):
        self.app = app
        self.current_session = None

    def normalize_profile_url(self, raw_url: str) -> str:
        url = raw_url.strip()
        if url and "://" not in url:
            # Scheme-less URLs would parse with an empty host, bypassing
            # both the dead-site check and the host-based routing.
            url = "https://" + url
        return url

    def validate_profile_url(self, raw_url: str) -> tuple[str, Optional[str]]:
        """Validate a URL before it enters the profile list.

        Returns (normalized_url, reason); reason is None when valid.
        """
        url = self.normalize_profile_url(raw_url)
        if not url:
            return url, self.app.tr("PLEASE_ENTER_VALID_URL")

        parsed = self.app.url_service.parse_download_url(url)

        dead_site = self._find_dead_site(parsed.host)
        if dead_site:
            return url, self._dead_site_message(parsed.host, dead_site)

        if parsed.site_type == "unknown":
            return url, self.app.tr("INVALID_URL")

        return url, None

    def build_request_from_ui(self) -> DownloadRequest:
        url = self.normalize_profile_url(self.app.download_panel.url_input.text())
        return self._build_request_for_url(url)

    def _build_request_for_url(self, url: str) -> DownloadRequest:
        panel = self.app.download_panel
        return DownloadRequest(
            url=url,
            download_folder=self.app.download_folder,
            download_images=panel.images_check.isChecked(),
            download_videos=panel.videos_check.isChecked(),
            download_compressed=panel.compressed_check.isChecked(),
            only_this_url=panel.only_this_url_check.isChecked(),
        )

    def _create_downloader(self, cls, request: DownloadRequest, **extra):
        settings = getattr(self.app, "settings", None) or {}
        kwargs = {
            "download_folder": request.download_folder,
            "log_callback": self.app.add_log_message_safe,
            "enable_widgets_callback": self.app.enable_widgets,
            "update_progress_callback": self.app.update_progress,
            "update_global_progress_callback": self.app.update_global_progress,
            "download_images": request.download_images,
            "download_videos": request.download_videos,
            "download_compressed": request.download_compressed,
            "folder_structure": settings.get("folder_structure", "default"),
            "rate_limit_interval": float(settings.get("rate_limit_interval", 0.0) or 0.0),
            "tr": self.app.tr,
            "max_workers": self.app.max_downloads,
            "max_retries": int(settings.get("max_retries", 3) or 3),
            "retry_interval": float(settings.get("retry_interval", 2.0) or 2.0),
            **extra,
        }
        downloader = cls(**kwargs)
        downloader.file_naming_mode = settings.get("file_naming_mode", 0)
        self.app.active_downloader = downloader
        return downloader

    def start_download(self):
        self.start_session(None)

    def start_session(self, queue_items: Optional[list] = None):
        """Start a sequential download session.

        With queue_items=None a one-item session is built from the URL
        input, which keeps the classic single-URL flow on the same code
        path as multi-profile sessions.
        """
        if queue_items is None:
            request = self.build_request_from_ui()

            if not request.download_folder:
                self.app.show_error(
                    self.app.tr("ERROR"),
                    self.app.tr("PLEASE_SELECT_DOWNLOAD_FOLDER")
                )
                return

            if not request.url:
                self.app.show_error(
                    self.app.tr("ERROR"),
                    self.app.tr("PLEASE_ENTER_VALID_URL")
                )
                return

            items = [ProfileQueueItem(url=request.url, request=request)]
        else:
            items = [
                item for item in queue_items
                if item.status is ProfileStatus.PENDING
            ]

            if not items:
                self.app.show_error(
                    self.app.tr("ERROR"),
                    self.app.tr("PLEASE_ENTER_VALID_URL")
                )
                return

            if not self.app.download_folder:
                self.app.show_error(
                    self.app.tr("ERROR"),
                    self.app.tr("PLEASE_SELECT_DOWNLOAD_FOLDER")
                )
                return

            # The global options are frozen here, once per session; the
            # runner never re-reads the UI between profiles.
            for item in items:
                item.request = self._build_request_for_url(item.url)

        self.app.prepare_download_ui()

        session = ProfileSession(items)
        self.current_session = session
        self.app.set_session_active(True)

        session_thread = threading.Thread(
            target=self._run_session,
            args=(session,),
            daemon=True
        )
        session_thread.start()
        self.app.download_thread = session_thread

    def _run_session(self, session: ProfileSession):
        total = len(session.items)
        any_job_started = False
        last_downloader = None

        try:
            for index, item in enumerate(session.items):
                if session.cancel_requested.is_set():
                    self._mark_item(session, index, ProfileStatus.CANCELLED)
                    continue

                session.current_index = index

                if index > 0:
                    self.app.reset_progress_between_profiles()

                if total > 1:
                    self.app.notify_session_progress(index + 1, total)
                    self.app.add_log_message_safe(
                        self.app.tr(
                            "SESSION_PROFILE_LOG",
                            current=index + 1,
                            total=total,
                            url=item.url,
                        )
                    )

                self._mark_item(session, index, ProfileStatus.DOWNLOADING)

                job, error = self._resolve_download_job(item.request)

                if error is not None:
                    if total == 1 and error.show_popup:
                        self.app.show_error(self.app.tr("ERROR"), error.message)
                    self._mark_item(session, index, ProfileStatus.ERROR, error.message)
                    continue

                # Cancel may land while the job is being resolved, before
                # cancel_download can reach the new downloader; without
                # this check that profile would run with no cancel signal.
                if session.cancel_requested.is_set():
                    if self.app.active_downloader is job.downloader:
                        self.app.active_downloader = None
                    self._mark_item(session, index, ProfileStatus.CANCELLED)
                    continue

                any_job_started = True
                last_downloader = job.downloader

                try:
                    job.method(*job.args)
                except Exception as e:
                    self.app.add_log_message_safe(
                        self.app.tr("DOWNLOAD_THREAD_ERROR", error=e)
                    )
                    self._mark_item(session, index, ProfileStatus.ERROR, str(e))
                else:
                    if session.cancel_requested.is_set():
                        self._mark_item(session, index, ProfileStatus.CANCELLED)
                    else:
                        # Downloaders swallow per-file failures; their
                        # counters are the only record of what happened.
                        failed_count, completed_count = self._collect_file_stats(
                            job.downloader
                        )

                        if failed_count and not completed_count:
                            self._mark_item(
                                session,
                                index,
                                ProfileStatus.ERROR,
                                self.app.tr(
                                    "PROFILE_ALL_FILES_FAILED", failed=failed_count
                                ),
                            )
                        elif failed_count:
                            self._mark_item(
                                session,
                                index,
                                ProfileStatus.COMPLETED,
                                self.app.tr(
                                    "PROFILE_SOME_FILES_FAILED", failed=failed_count
                                ),
                            )
                        else:
                            self._mark_item(session, index, ProfileStatus.COMPLETED)
                finally:
                    # Only clear if a newer download has not replaced it already
                    if self.app.active_downloader is job.downloader:
                        self.app.active_downloader = None
        finally:
            summary = self._build_summary(session)

            if total > 1:
                self.app.add_log_message_safe(
                    self.app.tr(
                        "SESSION_SUMMARY_COMPLETED",
                        completed=summary["completed"],
                        total=summary["total"],
                    )
                )

            self.current_session = None
            self.app.set_session_active(False)
            self.app.enable_widgets()
            if any_job_started:
                self.app.export_logs(last_downloader)
            self.app.notify_session_finished(summary)

    def _mark_item(self, session: ProfileSession, index: int, status: ProfileStatus, reason: str = ""):
        item = session.items[index]
        item.status = status
        item.error_reason = reason or ""
        self.app.notify_session_item_changed(index, status.value, item.error_reason)

    def _collect_file_stats(self, downloader) -> tuple[int, int]:
        failed = getattr(downloader, "failed_files", None)
        failed_count = len(failed) if hasattr(failed, "__len__") else 0
        skipped = getattr(downloader, "skipped_files", None)
        skipped_count = len(skipped) if hasattr(skipped, "__len__") else 0

        try:
            completed_count = int(getattr(downloader, "completed_files", 0)) + skipped_count
        except (TypeError, ValueError):
            completed_count = skipped_count

        return failed_count, completed_count

    def _build_summary(self, session: ProfileSession) -> dict:
        return {
            "total": len(session.items),
            "completed": sum(
                1 for item in session.items
                if item.status is ProfileStatus.COMPLETED
            ),
            "failed": [
                (item.url, item.error_reason)
                for item in session.items
                if item.status is ProfileStatus.ERROR
            ],
            "partial": [
                (item.url, item.error_reason)
                for item in session.items
                if item.status is ProfileStatus.COMPLETED and item.error_reason
            ],
            "cancelled": [
                item.url
                for item in session.items
                if item.status is ProfileStatus.CANCELLED
            ],
        }

    def _find_dead_site(self, host: str) -> Optional[str]:
        return next(
            (d for d in DEAD_SITES if host == d or host.endswith("." + d)),
            None,
        )

    def _dead_site_message(self, host: str, dead_site: str) -> str:
        alternative = DEAD_SITES[dead_site]
        if alternative:
            return self.app.tr(
                "SITE_NO_LONGER_SUPPORTED_WITH_ALTERNATIVE",
                site=host,
                alternative=alternative,
            )
        return self.app.tr("SITE_NO_LONGER_SUPPORTED", site=host)

    def _resolve_download_job(
        self, request: DownloadRequest
    ) -> tuple[Optional[DownloadJob], Optional[JobResolutionError]]:
        """Resolve a request into the downloader call that serves it.

        Returns (job, error); exactly one is not None. Logs here, but
        error dialogs are the caller's call: the session runner records
        failures instead of popping one dialog per profile.
        """
        parsed = self.app.url_service.parse_download_url(request.url)

        host = parsed.host
        dead_site = self._find_dead_site(host)

        if dead_site:
            message = self._dead_site_message(host, dead_site)
            self.app.add_log_message_safe(message)
            return None, JobResolutionError(message)

        settings = getattr(self.app, "settings", None) or {}

        if parsed.site_type == "erome":
            self.app.add_log_message_safe(self.app.tr("DOWNLOADING_EROME"))
            dl = self._create_downloader(
                EromeDownloader,
                request,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36",
                    "Referer": "https://www.erome.com/",
                },
                download_images=request.download_images,
                download_videos=request.download_videos,
                is_profile_download=parsed.is_profile,
            )
            if parsed.is_album:
                self.app.add_log_message_safe(self.app.tr("ALBUM_URL"))
                method = dl.process_album_page
            else:
                self.app.add_log_message_safe("erome", self.app.tr("PROFILE_URL"))
                method = dl.process_profile_page
            return DownloadJob(
                downloader=dl,
                method=method,
                args=(request.url, request.download_folder, request.download_images, request.download_videos),
            ), None

        if parsed.site_type == "bunkr":
            self.app.add_log_message_safe("bunkr", self.app.tr("DOWNLOADING_BUNKR"))
            dl = self._create_downloader(
                BunkrDownloader,
                request,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
                    "Referer": "https://bunkr.site/",
                },
            )
            if parsed.is_post:
                self.app.add_log_message_safe(self.app.tr("POST_URL"))
                method = dl.descargar_post_bunkr
            else:
                self.app.add_log_message_safe("bunkr", self.app.tr("PROFILE_URL"))
                method = dl.descargar_perfil_bunkr
            return DownloadJob(downloader=dl, method=method, args=(request.url,)), None

        if parsed.site_type == "coomer_kemono":
            self.app.add_log_message_safe(self.app.tr("STARTING_DOWNLOAD"))
            dl = self._create_downloader(
                Downloader,
                request,
                headers={"User-Agent": USER_AGENT, "Referer": "https://pawchive.pw/"},
                download_images=request.download_images,
                download_videos=request.download_videos,
                download_compressed=request.download_compressed,
                folder_structure=settings.get("folder_structure", "default"),
                rate_limit_interval=float(settings.get("rate_limit_interval", 0.0) or 0.0),
            )
            site, service, user, post = parsed.host, parsed.service, parsed.user, parsed.post
            if service is None or user is None:
                message = self.app.tr("FAILED_TO_EXTRACT_SERVICE" if service is None else "FAILED_TO_EXTRACT_USER_ID")
                self.app.add_log_message_safe(message)
                self.app.add_log_message_safe("SYSTEM", self.app.tr("INVALID_URL"))
                return None, JobResolutionError(message)

            self.app.add_log_message_safe(self.app.tr("EXTRACTED_SERVICE_SITE", service=service, site=site))
            if parsed.is_post:
                self.app.add_log_message_safe(self.app.tr("DOWNLOADING_SINGLE_POST"))
                return DownloadJob(downloader=dl, method=dl.download_single_post, args=(site, post, service, user)), None

            self.app.add_log_message_safe(self.app.tr("DOWNLOADING_ALL_USER_CONTENT"))
            return DownloadJob(
                downloader=dl,
                method=dl.download_media,
                args=(site, user, service, parsed.query, True, parsed.offset, request.only_this_url),
            ), None

        if parsed.site_type == "simpcity":
            self.app.add_log_message_safe(self.app.tr("DOWNLOADING_SIMPCITY"))
            dl = self._create_downloader(SimpCity, request)
            return DownloadJob(
                downloader=dl,
                method=dl.download_images_from_simpcity,
                args=(request.url, not request.only_this_url),
            ), None

        if parsed.site_type == "coomerfans":
            self.app.add_log_message_safe(self.app.tr("DOWNLOADING_COOMERFANS"))
            dl = self._create_downloader(
                CoomerfansDownloader,
                request,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
                    "Referer": "https://coomerfans.com/",
                },
                download_images=request.download_images,
                download_videos=request.download_videos,
                is_profile_download=parsed.is_profile,
                folder_structure=settings.get("folder_structure", "default"),
                rate_limit_interval=float(settings.get("rate_limit_interval", 0.0) or 0.0),
            )
            if parsed.is_post:
                self.app.add_log_message_safe(self.app.tr("POST_URL"))
                return DownloadJob(
                    downloader=dl,
                    method=dl.process_post_page,
                    args=(request.url, request.download_folder, request.download_images, request.download_videos),
                ), None
            self.app.add_log_message_safe(self.app.tr("PROFILE_URL"))
            return DownloadJob(
                downloader=dl,
                method=dl.process_profile_page,
                args=(request.url, request.download_folder, request.download_images, request.download_videos, request.only_this_url),
            ), None

        message = self.app.tr("INVALID_URL")
        self.app.add_log_message_safe(message)
        return None, JobResolutionError(message, show_popup=False)

    def request_session_cancel(self):
        session = self.current_session
        if session is not None:
            session.cancel_requested.set()

    def cancel_download(self):
        # The session flag goes first: between profiles active_downloader
        # is None and the flag is all that stops the next profile.
        had_session = self.current_session is not None
        self.request_session_cancel()

        if self.app.active_downloader:
            try:
                self.app.active_downloader.request_cancel()
            except Exception:
                pass
            self.app.active_downloader = None
            self.app.clear_progress_bars()
        elif had_session:
            self.app.add_log_message_safe(self.app.tr("SESSION_CANCEL_REQUESTED"))
        else:
            self.app.add_log_message_safe(self.app.tr("NO_ACTIVE_DOWNLOAD_TO_CANCEL"))

        self.app.enable_widgets()
