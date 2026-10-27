"""Translate coomer.st links into their coomerfans.com equivalents.

Coomer blocks downloaders, but coomerfans mirrors the same creators under
its own IDs, so links cannot be rewritten directly:

    coomer.st/onlyfans/user/mrselliebrooks
    -> coomerfans.com/u/onlyfans/328453/mrselliebrooks

Profiles are found with the coomerfans search page (exact service + name
match). Posts are matched by their publish timestamp, which both sites
share, using a binary search over the model's date-ordered profile pages.
Coomer's API is only used for metadata (names for numeric IDs, post
timestamps), which it still serves.
"""

import math
import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import quote, unquote, urlencode, urlparse

import requests

from downloader.adapters.http_retry import RetryingScraper, ScrapeCancelled
from downloader.adapters.resolution_cache import ResolutionCache

COOMER_HOSTS = ("coomer.st", "coomer.su", "coomer.party")
# Older coomer domains no longer resolve; the API is only reachable here.
COOMER_API = "https://coomer.st/api/v1"
COOMERFANS = "https://coomerfans.com"
COOMERFANS_PAGE_SIZE = 30
# Search results are unordered, so short names ("anna": ~2000 matches,
# 32 pages) may need every page. Pages are fetched in parallel.
MAX_SEARCH_PAGES = 80
SEARCH_WORKERS = 4
CACHE_TTL_SECONDS = 30 * 24 * 3600

_COOMER_PATH_RE = re.compile(r"^/([^/]+)/user/([^/?#]+)(?:/post/([^/?#]+))?")
_PROFILE_HREF_RE = re.compile(r"^/u/([^/]+)/(\d+)/([^/?#]+)")
_TOTAL_RE = re.compile(r"Total\s+(\d+)")
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)


class RedirectError(Exception):
    """Carries a translated, user-facing reason."""


def is_coomer_host(host):
    host = (host or "").lower()
    return any(host == h or host.endswith("." + h) for h in COOMER_HOSTS)


def _timestamp(value):
    """'2025-09-15T22:07:16' and '2025-09-15 22:07:16 +0000 UTC' -> '2025-09-15 22:07:16'."""
    return str(value or "").replace("T", " ").strip()[:19]


class CoomerRedirector(RetryingScraper):
    site_name = "coomer"

    def __init__(self, session=None, log_callback=None, tr=None, should_cancel=None,
                 cache_db_path="resources/config/downloads.db",
                 max_retries=3, retry_interval=2.0, request_interval=0.5):
        self.session = session or requests.Session()
        self.headers = {
            "User-Agent": _BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": COOMERFANS + "/",
        }
        # Coomer's API answers "Accept: text/css" requests.
        self.api_headers = {"User-Agent": _BROWSER_UA, "Accept": "text/css"}
        self.log_callback = log_callback
        self.tr = tr if tr else (lambda key, **kw: key.format(**kw) if kw else key)
        self._cache = ResolutionCache("coomer_redirect_cache", db_path=cache_db_path,
                                      ttl_seconds=CACHE_TTL_SECONDS)
        self._init_retry(max_retries=max_retries, retry_interval=retry_interval,
                         request_interval=request_interval, should_cancel=should_cancel)

    def log(self, key, **kwargs):
        if self.log_callback:
            self.log_callback(self.site_name, self.tr(key, **kwargs))

    def _fail(self, key, **kwargs):
        return RedirectError(self.tr(key, **kwargs))

    # ------------------------------------------------------------------
    def resolve(self, url):
        """Return the coomerfans URL for a coomer profile or post link."""
        parsed = urlparse(url)
        match = _COOMER_PATH_RE.match(parsed.path)
        if not match:
            raise self._fail("COOMER_REDIRECT_INVALID", url=url)

        service, user, post_id = (unquote(g) if g else g for g in match.groups())
        api_base = f"{COOMER_API}/{quote(service)}/user/{quote(user, safe='')}"

        cache_key = f"{service}:{user}:{post_id or ''}"
        cached = self._cache.load(cache_key)
        if cached and cached.get("url"):
            return cached["url"]

        self.log("COOMER_REDIRECT_RESOLVING", url=url)
        creator = None
        if user.isdigit() or post_id:
            creator = self._creator(api_base, service, user, required=user.isdigit())
        name = (creator or {}).get("name") or user

        profile_url = self._find_profile(service, name)
        if not profile_url:
            raise self._fail("COOMER_REDIRECT_PROFILE_NOT_FOUND", name=name, service=service)
        self._cache.store(f"{service}:{user}:", {"url": profile_url})

        target = profile_url
        if post_id:
            target = self._find_post(api_base, post_id, profile_url, (creator or {}).get("post_count"))
            self._cache.store(cache_key, {"url": target})

        self.log("COOMER_REDIRECT_DONE", url=url, target=target)
        return target

    # ------------------------------------------------------------------
    def _creator(self, api_base, service, user, required):
        try:
            data = self._request(api_base + "/profile", headers=self.api_headers).json()
            if isinstance(data, dict) and data.get("name"):
                return data
            error = (data or {}).get("error") if isinstance(data, dict) else data
        except requests.RequestException as e:
            error = e
        except ValueError as e:
            error = e
        if required:
            raise self._fail("COOMER_REDIRECT_NAME_FAILED", service=service, user=user, error=error)
        return None

    def _search_page(self, service, name, page):
        """Return (total_results, result_count, match_url) for one search page."""
        params = {"q": name, "service": service}
        if page > 1:
            params["page"] = page
        soup = self._request_soup(f"{COOMERFANS}/?{urlencode(params)}")

        heading = soup.find("h2", string=_TOTAL_RE)
        total = int(_TOTAL_RE.search(heading.get_text()).group(1)) if heading else None
        # With no results the page lists unrelated creators instead.
        if total == 0:
            return 0, 0, None
        container = heading.find_parent("section") if heading else None
        links = (container or soup).find_all("a", href=_PROFILE_HREF_RE)

        wanted = name.casefold()
        match = None
        for link in links:
            svc, uid, uname = _PROFILE_HREF_RE.match(link["href"]).groups()
            if svc == service and unquote(uname).casefold() == wanted:
                match = f"{COOMERFANS}/u/{svc}/{uid}/{uname}"
                break
        return total, len(links), match

    def _find_profile(self, service, name):
        total, per_page, match = self._search_page(service, name, 1)
        if match or not total or not per_page:
            return match

        pages = min(math.ceil(total / per_page), MAX_SEARCH_PAGES)
        remaining = list(range(2, pages + 1))
        if not remaining:
            return None

        executor = ThreadPoolExecutor(max_workers=SEARCH_WORKERS)
        failed_pages = 0
        last_error = None
        try:
            pending = {executor.submit(self._search_page, service, name, p) for p in remaining}
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    try:
                        match = future.result()[2]
                    except ScrapeCancelled:
                        raise
                    except Exception as e:
                        # The match may still be on another page.
                        failed_pages += 1
                        last_error = e
                        continue
                    if match:
                        return match
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if failed_pages:
            raise self._fail(
                "COOMER_REDIRECT_SEARCH_INCOMPLETE",
                name=name,
                service=service,
                failed=failed_pages,
                error=last_error,
            )
        return None

    def _profile_page_posts(self, profile_url, page, cache):
        if page not in cache:
            url = profile_url if page == 1 else f"{profile_url}?page={page}"
            soup = self._request_soup(url)
            posts = []
            for block in soup.select("div.post"):
                link = block.find("a", href=re.compile(r"^/p/\d+"))
                date = block.find(class_="post-date")
                if not (link and date):
                    continue
                posts.append({
                    "url": COOMERFANS + link["href"],
                    "date": _timestamp(date.get_text()),
                    "title": link.get_text(strip=True),
                })
            cache[page] = posts
        return cache[page]

    def _find_post(self, api_base, post_id, profile_url, post_count):
        try:
            data = self._request(f"{api_base}/post/{quote(post_id)}", headers=self.api_headers).json()
            post = data.get("post", data) if isinstance(data, dict) else {}
        except (requests.RequestException, ValueError) as e:
            raise self._fail("COOMER_REDIRECT_POST_FAILED", post=post_id, error=e) from None

        target = _timestamp(post.get("published") or post.get("added"))
        if not target:
            raise self._fail("COOMER_REDIRECT_POST_NOT_FOUND", post=post_id, profile=profile_url)
        title = (post.get("title") or "").strip()

        # Pages are newest first. Find the first page whose oldest post is
        # not newer than the target; out-of-range pages repeat the last page
        # (or are empty), so overshooting the upper bound is harmless.
        pages = {}
        lo = 1
        hi = math.ceil(int(post_count) / COOMERFANS_PAGE_SIZE) + 3 if post_count else 400
        while lo < hi:
            mid = (lo + hi) // 2
            posts = self._profile_page_posts(profile_url, mid, pages)
            if not posts or min(p["date"] for p in posts) <= target:
                hi = mid
            else:
                lo = mid + 1

        matches, seen = [], set()
        # Posts sharing a timestamp can straddle a page boundary.
        for page in (lo, lo + 1):
            for p in self._profile_page_posts(profile_url, page, pages):
                if p["date"] == target and p["url"] not in seen:
                    seen.add(p["url"])
                    matches.append(p)

        if not matches:
            raise self._fail("COOMER_REDIRECT_POST_NOT_FOUND", post=post_id, profile=profile_url)
        for p in matches:
            if title and p["title"] == title:
                return p["url"]
        return matches[0]["url"]
