from urllib.parse import quote_plus, urlencode, urljoin, urlparse
import os
import requests

from downloader.core.base_api_downloader import BaseApiDownloader


class Downloader(BaseApiDownloader):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.domain_name = "coomer"

    def get_domain_name(self, site):
        if "pawchive" in site:
            return "pawchive"
        return "kemono" if "kemono" in site else "coomer"

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

            try:
                response = self.session.get(api_url, headers=self.headers, timeout=self.request_timeout)
                if response.status_code == 400:
                    self.log("CK_INVALID_REQUEST_OR_USER_NOT_FOUND", site=site, user_id=user_id, service=service)
                    return all_posts

                response.raise_for_status()
                posts = response.json()

                if not posts:
                    return all_posts

                for post in posts:
                    if self.cancel_requested.is_set():
                        return all_posts

                    if specific_post_id and str(post.get("id")) == str(specific_post_id):
                        return [post]

                    all_posts.append(post)

                if specific_post_id or only_first_page:
                    return all_posts

                if len(posts) < 50:
                    break

                offset += 50

            except requests.exceptions.RequestException as e:
                self.log("CK_FAILED_TO_FETCH_USER_POSTS", error=e)
                break

        return all_posts

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
        request_url = media_url
        response = self.safe_request(request_url, max_retries=self.max_retries)

        if response is None:
            fallback_url = self._pawchive_preview_fallback(media_url)
            if fallback_url:
                self.log("PAWCHIVE_PREVIEW_FALLBACK", media_url=media_url)
                request_url = fallback_url

        self.process_media_element(
            request_url,
            user_id=user_id,
            post_id=post_id,
            post_name=post_name,
            post_time=post_time,
            download_id=download_id,
            record_url=media_url,
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
