import os
import re
import uuid
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from downloader.adapters.http_retry import RetryingScraper, ScrapeCancelled
from downloader.adapters.resolution_cache import ResolutionCache

# Safety stop for profile pagination; real profiles are far smaller.
MAX_PROFILE_PAGES = 500


class EromeAdapter(RetryingScraper):
    site_name = "erome"

    def __init__(self, session, headers=None, log_callback=None, tr=None,
                 should_cancel=None, cache_db_path="resources/config/downloads.db",
                 max_retries=3, retry_interval=2.0, request_interval=0.0):
        self.session = session
        self.headers = {
            k: str(v).encode("ascii", "ignore").decode("ascii")
            for k, v in (headers or {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            }).items()
        }
        self.log_callback = log_callback
        self.tr = tr if tr else (lambda x, **kwargs: x.format(**kwargs) if kwargs else x)
        self._album_cache = ResolutionCache("erome_album_cache", db_path=cache_db_path)
        self._cache_hits = 0
        self._cache_misses = 0
        # Set when part of a profile could not be loaded.
        self.scrape_incomplete = False
        self._init_retry(
            max_retries=max_retries,
            retry_interval=retry_interval,
            request_interval=request_interval,
            should_cancel=should_cancel,
        )

    def log(self, message, **kwargs):
        if kwargs:
            message = self.tr(message, **kwargs)
        else:
            message = self.tr(message)
        if self.log_callback:
            self.log_callback(self.site_name, message)

    @staticmethod
    def clean_filename(filename):
        return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(filename).split("?")[0]).strip().rstrip(" .")

    @staticmethod
    def _page_url(url, page):
        parsed = urlparse(url)
        query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != "page"]
        query.append(("page", str(page)))
        return urlunparse(parsed._replace(query=urlencode(query)))

    def _collect_album_urls(self, profile_url, soup, only_first_page):
        """Album links from every page of the profile, in order."""
        album_urls = []
        seen = set()
        page = 1

        while True:
            new_links = 0
            for album_link in soup.find_all("a", class_="album-link"):
                href = album_link.get("href")
                if not href:
                    continue
                album_url = urljoin(profile_url, href)
                if album_url in seen:
                    continue
                seen.add(album_url)
                album_urls.append(album_url)
                new_links += 1

            # A page with nothing new is past the end (or the site clamped
            # the page number back to an earlier page).
            if only_first_page or not new_links or page >= MAX_PROFILE_PAGES or self._cancelled():
                return album_urls

            page += 1
            try:
                soup = self._request_soup(self._page_url(profile_url, page))
            except ScrapeCancelled:
                return album_urls
            except Exception as e:
                if getattr(getattr(e, "response", None), "status_code", None) == 404:
                    return album_urls  # past the last page
                self.log("EROME_PROFILE_PAGE_FAILED", page=page, error=e)
                self.scrape_incomplete = True
                return album_urls

    def _resolve_profile(self, profile_url, soup=None, download_images=True, download_videos=True,
                         only_first_page=False):
        soup = soup or self._request_soup(profile_url)

        username_tag = soup.find("h1", class_="username")
        username = username_tag.get_text(strip=True) if username_tag else ""
        base_folder_name = self.clean_filename(username) or self.clean_filename(self.tr("EROME_UNKNOWN_PROFILE"))

        media = []
        self._cache_hits = 0
        self._cache_misses = 0
        self.scrape_incomplete = False
        album_urls = self._collect_album_urls(profile_url, soup, only_first_page)

        for album_url in album_urls:
            if self._cancelled():
                break

            try:
                album_data = self._resolve_album(
                    album_url,
                    soup=None,
                    download_images=download_images,
                    download_videos=download_videos,
                    inherited_base_folder=base_folder_name,
                )
                media.extend(album_data["media"])
            except ScrapeCancelled:
                break
            except Exception as e:
                self.log("EROME_ERROR_RESOLVING_ALBUM", url=album_url, error=e)
                self.scrape_incomplete = True

        if self._cache_hits or self._cache_misses:
            self.log(
                "EROME_CACHE_SUMMARY",
                cached=self._cache_hits,
                scraped=self._cache_misses,
            )

        return {
            "mode": "profile",
            "folder_name": base_folder_name,
            "media": media,
        }

    def _scrape_album_media(self, soup, album_url):
        """
        Scrapes every video and image of an album page, regardless of the
        current download filters, so the result can be cached and reused
        with any filter combination.
        Returns a list of {"media_url", "resource_type", "filename"} dicts.
        """
        items = []
        seen_urls = set()

        for video in soup.find_all("video"):
            source = video.find("source")
            if not source:
                continue
            src = source.get("src")
            if not src:
                continue

            abs_video_src = urljoin(album_url, src)
            if abs_video_src in seen_urls:
                continue
            seen_urls.add(abs_video_src)

            items.append({
                "media_url": abs_video_src,
                "resource_type": "Video",
                "filename": self.clean_filename(os.path.basename(abs_video_src.split("?")[0])),
            })

        for div in soup.select("div.img"):
            img = (
                div.find("img", attrs={"data-src": True})
                or div.find("img", attrs={"src": True})
            )
            if not img:
                continue

            raw_src = img.get("data-src") or img.get("src")
            if not raw_src:
                continue

            abs_img_src = urljoin(album_url, raw_src)
            lower_src = abs_img_src.lower()

            if lower_src.startswith("data:"):
                continue
            if any(x in lower_src for x in ["/avatar/", "/users/", "/profile/", "/static/", "/assets/", "/images/"]):
                continue
            if any(x in lower_src for x in ["bg.jpg", "background", "avatar", "cover", "logo", "icon", "banner", "profile"]):
                continue
            if abs_img_src in seen_urls:
                continue

            seen_urls.add(abs_img_src)

            filename = os.path.basename(abs_img_src.split("?")[0])
            if not filename:
                filename = f"image_{uuid.uuid4().hex[:8]}.jpg"

            items.append({
                "media_url": abs_img_src,
                "resource_type": "Image",
                "filename": self.clean_filename(filename),
            })

        return items

    def _resolve_album(
        self,
        album_url,
        soup=None,
        download_images=True,
        download_videos=True,
        inherited_base_folder=None,
    ):
        cached = None
        if soup is None:
            cached = self._album_cache.load(album_url)
            if not (isinstance(cached, dict) and isinstance(cached.get("items"), list)):
                cached = None

        if cached is None:
            self._cache_misses += 1
            soup = soup or self._request_soup(album_url)
            title_tag = soup.find("h1")
            album_title = title_tag.get_text(strip=True) if title_tag else ""
            cached = {
                "album_title": album_title,
                "items": self._scrape_album_media(soup, album_url),
            }
            # An empty result may be a page that failed to render; caching
            # it would hide the album until the entry expires.
            if cached["items"]:
                self._album_cache.store(album_url, cached)
        else:
            self._cache_hits += 1

        album_folder_name = (
            self.clean_filename(cached.get("album_title") or "")
            or self.clean_filename(self.tr("EROME_UNKNOWN_ALBUM"))
        )

        if inherited_base_folder:
            effective_folder = os.path.join(inherited_base_folder, album_folder_name)
        else:
            effective_folder = album_folder_name

        media = []
        for item in cached["items"]:
            resource_type = item.get("resource_type")
            if resource_type == "Video" and not download_videos:
                continue
            if resource_type == "Image" and not download_images:
                continue

            media.append({
                "media_url": item.get("media_url"),
                "post_id": None,
                "title": album_folder_name,
                "published": "",
                "folder_name": effective_folder,
                "resource_type": resource_type,
                "filename": item.get("filename"),
            })

        return {
            "mode": "album",
            "folder_name": effective_folder,
            "media": media,
        }
