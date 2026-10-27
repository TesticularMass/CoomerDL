import datetime
import threading
from pathlib import Path
from html import escape


class LogService:
    def __init__(self, log_folder="resources/config/logs/"):
        self.log_folder = Path(log_folder)
        self.all_logs = []
        self._lock = threading.Lock()

    DOMAIN_COLORS = {
        "coomer": "#4FC3F7",
        "coomerfans": "#4DB6AC",
        "kemono": "#81C784",
        "pawchive": "#A1887F",
        "erome": "#F48FB1",
        "bunkr": "#FFB74D",
        "simpcity": "#BA68C8",
        "jpg5": "#FFD54F",
        "system": "#FDFDFD",
        "error": "#EF5350",
        "warning": "#FFA726",
    }

    def get_domain_color(self, domain: str) -> str:
        return self.DOMAIN_COLORS.get(domain.lower(), "#90A4AE")

    def format_plain(self, domain: str, message: str) -> str:
        return f"{domain}: {message}"

    def format_html(self, domain: str, message: str) -> str:
        color = self.get_domain_color(domain)
        domain_escaped = escape(domain)
        message_escaped = escape(message)

        return (
            f'<div style="margin:2px 0; padding:4px 8px; border-left:4px solid {color};">'
            f'<span style="color:{color}; font-weight:600;">{domain_escaped}:</span> '
            f'<span>{message_escaped}</span>'
            f'</div>'
        )

    def add_domain_log(self, domain: str, message: str):
        plain = self.format_plain(domain, message)
        with self._lock:
            self.all_logs.append(plain)
        return self.format_html(domain, message)

    def clear_runtime(self):
        with self._lock:
            self.all_logs.clear()

    def export_logs(
        self,
        active_downloader=None,
        download_images_enabled=True,
        download_videos_enabled=True,
        download_start_time=None,
    ):
        self.log_folder.mkdir(parents=True, exist_ok=True)
        log_file_path = self.log_folder / f"log_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

        total_files = getattr(active_downloader, "total_files", 0) if active_downloader else 0
        completed_files = getattr(active_downloader, "completed_files", 0) if active_downloader else 0
        skipped_files = list(getattr(active_downloader, "skipped_files", [])) if active_downloader else []
        failed_files = list(getattr(active_downloader, "failed_files", [])) if active_downloader else []

        duration = datetime.datetime.now() - download_start_time if download_start_time else "N/A"
        nl = chr(10)
        skipped_files_summary = nl.join(skipped_files)
        failed_files_summary = nl.join(failed_files)

        with self._lock:
            logs_snapshot = list(self.all_logs)

        summary = (
            f"Total de archivos descargados: {completed_files} / {total_files}{nl}"
            f"Imágenes habilitadas: {download_images_enabled}{nl}"
            f"Videos habilitados: {download_videos_enabled}{nl}"
            f"Tiempo total de descarga: {duration}{nl}{nl}"
            f"Archivos saltados ({len(skipped_files)}):{nl}{skipped_files_summary}{nl}{nl}"
            f"Archivos fallidos ({len(failed_files)}):{nl}{failed_files_summary}{nl}{nl}"
        )

        with open(log_file_path, "w", encoding="utf-8") as file:
            file.write(summary)
            file.write(f"{nl}--- LOGS COMPLETOS ---{nl}")
            file.write(nl.join(logs_snapshot))

        return str(log_file_path)
