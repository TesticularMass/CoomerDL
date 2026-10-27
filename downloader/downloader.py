from urllib.parse import quote_plus, urlencode, urljoin, urlparse
import os
import requests

from downloader.core.base_api_downloader import BaseApiDownloader
from downloader.adapters.http_retry import RETRYABLE_STATUS_CODES


class Downloader(BaseApiDownloader):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.domain_name = "coomer"

    def get_domain_name(self, site):
        if "pawchive" in site:
            return "pawchive"
        return "kemono" if "kemono" in site else "coomer"

    def _fetch_posts_page(self, api_url, site, user_id, service):
        """
        One page of the post list, with retries. Returns the posts, [] when
        the user does not exist, or None when the page could not be loaded.
        """
        error = None
        for attempt in range(self.max_retries + 1):
            if self.cancel_requested.is_set():
                return None

            response = None
            try:
                response = self.session.get(api_url, headers=self.headers, timeout=self.request_timeout)
                if response.status_code == 400:
                    self.log("CK_INVALID_REQUEST_OR_USER_NOT_FOUND", site=site, user_id=user_id, service=service)
                    return []
                response.raise_for_status()
                posts = response.json()
                if isinstance(posts, list):
                    return posts
                # An error object instead of a list: retrying will not help.
                error = ValueError(str(posts)[:200])
                break
            except requests.exceptions.HTTPError as e:
                error = e
                if response.status_code not in RETRYABLE_STATUS_CODES:
                    break
            except (requests.exceptions.RequestException, ValueError) as e:
                error = e

            if attempt < self.max_retries:
                delay = self._compute_retry_delay(attempt, response)
                self.log(
                    "HTTP_RETRY_REQUEST",
                    attempt=attempt + 1,
                    total=self.max_retries + 1,
                    status_code=response.status_code if response is not None else type(error).__name__,
                    url=api_url,
                    delay=round(delay, 1),
                )
                if not self._sleep_cancellable(delay):
                    return None

        self.log("CK_FAILED_TO_FETCH_USER_POSTS", error=error)
        self.incomplete_reason = self._translate_text("PROFILE_LISTING_INCOMPLETE")
        return None

    def fetch_user_posts(
        self,
        site,
        user_id,
        service,
        query=None,
        specific_post_id=None,
        initial_offset=0,
        log_fetching=True,
        only_first_page=False,
    ):
        all_posts = []
        offset = initial_offset
        user_id_encoded = quote_plus(user_id)
        self.domain_name = self.get_domain_name(site)

        while True:
            if self.cancel_requested.is_set():
                return all_posts

            api_url = f"https://{site}/api/v1/{service}/user/{user_id_encoded}/posts"
            url_query = {"o": offset}
            if query not in (None, "", 0, "0"):
                url_query["q"] = query
            api_url += "?" + urlencode(url_query)

            if log_fetching:
                self.log("CK_FETCHING_USER_POSTS", api_url=api_url)

            posts = self._fetch_posts_page(api_url, site, user_id, service)
            if not posts:
                # End of the list, unknown user, or a page that kept
                # failing (logged and flagged as incomplete).
                return all_posts

            for post in posts:
                if self.cancel_requested.is_set():
                    return all_posts

                if specific_post_id:
                    if str(post.get("id")) == str(specific_post_id):
                        return [post]
                    continue

                all_posts.append(post)

            if only_first_page or len(posts) < 50:
                return all_posts

            offset += 50

    def process_post(self, post, site):
        is_pawchive = "pawchive" in site
        base = "https://file.pawchive.pw/" if is_pawchive else f"https://{site}/"

        def _full(entry):
            path = entry.get("path") or entry.get("url") or entry.get("name")
            if not path:
                return None
            p = path if str(path).startswith("/") else f"/{path}"
            if is_pawchive and not p.startswith("/data/"):
                p = f"/data{p}"

            if is_pawchive and entry.get("preview_only"):
                self.log(
                    "PAWCHIVE_PREVIEW_ONLY",
                    name=entry.get("name") or os.path.basename(p),
                )
                return urljoin("https://img.pawchive.pw/", f"/thumbnail{p}")

            return urljoin(base, p)

        media_urls = []

        main_file = post.get("file") or {}
        u = _full(main_file)
        if u:
            media_urls.append(u)

        for att in (post.get("attachments") or []):
            u = _full(att)
            if u:
                media_urls.append(u)

        return media_urls

    def _pawchive_preview_fallback(self, media_url):
        prefix = "https://file.pawchive.pw/data/"
        if media_url.startswith(prefix):
            return "https://img.pawchive.pw/thumbnail/data/" + media_url[len(prefix):]
        return None

    def _collect_filtered_media(self, posts, site):
        collected = []
        seen_media = set()

        for post in posts:
            current_post_id = post.get("id") or "unknown_id"
            title = post.get("title") or ""
            published_time = post.get("published") or ""

            media_urls = self.process_post(post, site)
            for media_url in media_urls:
                if media_url in seen_media:
                    continue

                seen_media.add(media_url)
                ext = os.path.splitext(urlparse(media_url).path)[1].lower()

                if (ext in self.image_extensions and not self.download_images) or                    (ext in self.video_extensions and not self.download_videos) or                    (ext in self.compressed_extensions and not self.download_compressed):
                    continue

                collected.append({
                    "media_url": media_url,
                    "post_id": current_post_id,
                    "title": title,
                    "published": published_time,
                })

        return collected

    def download_file(
        self,
        media_url,
        user_id,
        post_id=None,
        post_name=None,
        post_time=None,
        download_id=None,
    ):
        self.process_media_element(
            media_url,
            user_id=user_id,
            post_id=post_id,
            post_name=post_name,
            post_time=post_time,
            download_id=download_id,
            record_url=media_url,
            fallback_url=self._pawchive_preview_fallback(media_url),
        )

    def download_media(self, site, user_id, service, query=None, download_all=False, initial_offset=0, only_first_page=False):
        try:
            self.domain_name = self.get_domain_name(site)
            self.log("CK_STARTING_DOWNLOAD_PROCESS")

            posts = self.fetch_user_posts(
                site,
                user_id,
                service,
                query=query,
                initial_offset=initial_offset,
                log_fetching=download_all,
                only_first_page=only_first_page,
            )

            if not posts:
                self.log("CK_NO_POSTS_FOUND_FOR_USER")
                return

            if not download_all:
                posts = posts[:50]

            media_entries = self._collect_filtered_media(posts, site)
            self.run_batch(
                media_entries,
                lambda entry: self.download_file(
                    entry["media_url"],
                    user_id,
                    entry["post_id"],
                    entry["title"],
                    entry["published"],
                    entry["media_url"],
                ),
            )

        except Exception as e:
            self.log("CK_ERROR_DURING_DOWNLOAD", error=e)
        finally:
            self.shutdown_executor()

    def download_single_post(self, site, post_id, service, user_id):
        try:
            self.domain_name = self.get_domain_name(site)
            posts = self.fetch_user_posts(site, user_id, service, specific_post_id=post_id)
            if not posts:
                self.log("CK_NO_POST_FOUND_FOR_ID")
                return

            media_entries = self._collect_filtered_media(posts[:1], site)
            self.run_batch(
                media_entries,
                lambda entry: self.download_file(
                    entry["media_url"],
                    user_id,
                    entry["post_id"],
                    entry["title"],
                    entry["published"],
                    entry["media_url"],
                ),
            )

        except Exception as e:
            self.log("CK_ERROR_DURING_DOWNLOAD", error=e)
        finally:
            self.shutdown_executor()
