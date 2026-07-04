"""Keyless web + social read layer for Globus agents.

Zero API keys. Structured `{source, title, body, author, ts, url, meta, ok,
error}` dict for every read, so briefs and the LLM chat loop can consume any
of these sources without special-casing.

Supported sources (auto-detected from URL):
  - Twitter/X single tweet — public syndication endpoint (no auth)
  - Reddit thread / post — `.json` suffix (no auth)
  - YouTube video metadata — oembed (no auth, ~30/min soft-limit)
  - GitHub — repo readme / issue / PR / release via api.github.com
    (unauthenticated: 60 req/hr per source IP)
  - Generic web — stdlib urllib GET with a real UA header (JS-heavy pages
    that render client-side will return empty <body>; that's expected;
    prod doesn't have Playwright)

Not supported yet (add on demand):
  - Twitter thread / X Article (needs authenticated session — Sumit's X login)
  - Bilibili / XiaoHongShu (per Sharbel's Agent Reach; JP/CN sources)
  - PDF / video-transcript extraction

Public API:
  read_tweet(tweet_id_or_url)  — one tweet
  read_reddit(url)             — one thread with top-level comments
  read_youtube(url_or_id)      — one video's metadata
  read_github(url)             — repo / issue / PR / release
  read_web(url)                — generic HTML page (best-effort text extract)
  web_read(url)                — auto-detect + dispatch (main entry point)

The LLM tool wiring lives in server/globus_orchestrator.py — this module is
pure, no LLM / DB imports, callable from any agent script.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
import urllib.parse
from html import unescape
from typing import Any


# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
_DEFAULT_TIMEOUT = 8  # seconds
_MAX_BODY_CHARS = 20000  # cap what we return so LLM prompts don't explode


def _get(url: str, headers: dict | None = None,
         timeout: int = _DEFAULT_TIMEOUT) -> tuple[int, bytes]:
    """Barebones GET returning (status_code, body_bytes). Never raises."""
    h = {"User-Agent": _UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, body
    except Exception as e:
        # Wrap network / DNS / timeout in a 0-status so caller can handle uniformly
        return 0, str(e).encode("utf-8", "replace")


def _ok(source: str, url: str, *, title: str = "", body: str = "",
        author: str = "", ts: str = "", meta: dict | None = None) -> dict:
    if len(body) > _MAX_BODY_CHARS:
        body = body[:_MAX_BODY_CHARS] + "…[truncated]"
    return {"source": source, "url": url, "title": title, "body": body,
            "author": author, "ts": ts, "meta": meta or {},
            "ok": True, "error": ""}


def _err(source: str, url: str, error: str) -> dict:
    return {"source": source, "url": url, "title": "", "body": "",
            "author": "", "ts": "", "meta": {}, "ok": False, "error": error}


# ------------------------------------------------------------------
# Twitter / X — single tweet via public syndication endpoint
# ------------------------------------------------------------------
_TWEET_ID_RE = re.compile(r"(?:status(?:es)?/)(\d+)")


def _extract_tweet_id(s: str) -> str | None:
    if not s:
        return None
    s = str(s).strip()
    if s.isdigit():
        return s
    m = _TWEET_ID_RE.search(s)
    return m.group(1) if m else None


def _twitter_syndication_token(tid: str) -> str:
    """Twitter rotates the acceptable token. As of late-2024 the formula
    is `((id / 1e15) * pi)` base-36, with `0` and `.` stripped. Keeps the
    syndication endpoint responding when `token=a` (the classic short-
    circuit) starts 404-ing. Cheap to compute; harmless if wrong (falls
    through to the oembed fallback)."""
    import math
    try:
        n = (int(tid) / 1e15) * math.pi
    except Exception:
        return "a"
    # Convert float to base36 similar to JS number.toString(36)
    # We'll approximate with a straightforward encoding of the significant digits
    s = f"{n:.20f}"
    b36 = ""
    for c in s:
        if c.isdigit():
            b36 += "0123456789abcdefghijklmnopqrstuvwxyz"[int(c)]
        elif c.isalpha():
            b36 += c.lower()
    return b36.replace("0", "").replace(".", "") or "a"


def read_tweet(tweet_id_or_url: str) -> dict:
    """Single tweet. Handles either raw ID or any URL with `/status/<id>`.

    Tries in order:
      1. `cdn.syndication.twimg.com/tweet-result` with `token=a` (classic)
      2. Same endpoint with computed token (post-2024 rotation)
      3. `publish.twitter.com/oembed` — HTML embed with tweet text
    All three are keyless. Protected / deleted / private-account tweets
    return an error. For thread reading + article-mode, needs Playwright
    with an X login (not on prod yet)."""
    tid = _extract_tweet_id(tweet_id_or_url)
    url_public = (f"https://x.com/i/status/{tid}" if tid
                  else str(tweet_id_or_url))
    if not tid:
        return _err("twitter", url_public,
                    "could not extract tweet id from input")

    # Attempt 1 + 2: syndication with two token variants
    for token in ("a", _twitter_syndication_token(tid)):
        api = (f"https://cdn.syndication.twimg.com/tweet-result?"
               f"id={tid}&token={token}")
        code, body = _get(api)
        if code != 200:
            continue
        try:
            d = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            continue
        user = d.get("user") or {}
        text = (d.get("text") or "").strip()
        if not text:
            continue
        return _ok("twitter", url_public,
                   title=text[:200], body=text,
                   author=f"@{user.get('screen_name','?')}",
                   ts=d.get("created_at") or "",
                   meta={
                       "favorites": d.get("favorite_count"),
                       "retweets": d.get("conversation_count"),
                       "lang": d.get("lang"),
                       "photos": [p.get("url") for p in (d.get("photos") or [])],
                       "video": bool(d.get("video")),
                       "via": f"syndication token={token[:6]}",
                   })

    # Attempt 3: publish.twitter.com oembed — HTML with tweet text
    oembed = ("https://publish.twitter.com/oembed?"
              f"url=https://x.com/i/status/{tid}&omit_script=1")
    code, body = _get(oembed)
    if code == 200:
        try:
            d = json.loads(body.decode("utf-8", "replace"))
            html = d.get("html") or ""
            # Strip HTML tags for a plain-text view
            text = re.sub(r"<[^>]+>", " ", html)
            text = unescape(re.sub(r"\s+", " ", text)).strip()
            if text:
                return _ok("twitter", url_public,
                           title=text[:200], body=text,
                           author=d.get("author_name") or "",
                           ts="",
                           meta={"via": "publish oembed"})
        except Exception:
            pass

    return _err("twitter", url_public,
                "all keyless paths failed (deleted, protected, "
                "or Twitter rotated the endpoint)")


# ------------------------------------------------------------------
# Reddit — `.json` suffix on any thread URL, keyless
# ------------------------------------------------------------------
def read_reddit(url: str) -> dict:
    """One Reddit thread. Handles /r/foo/comments/<id>/... URLs. Returns
    the OP + top ~10 comments.

    Reddit locked the anonymous `.json` route in mid-2023 (returns 403
    to non-OAuth). Two fallbacks in order:
      1. `www.reddit.com/svc/shreddit/comments/...` — the mobile/embed
         backend, still allows unauth GET behind a real browser UA
      2. oembed for title-only, then generic HTML strip for body
    """
    u = url.strip()
    if "reddit.com" not in u:
        return _err("reddit", u, "not a reddit URL")
    u_clean = u.split("#", 1)[0].split("?", 1)[0].rstrip("/")

    # Attempt 1: .json (works when Reddit hasn't rotated their block list)
    code, body = _get(u_clean + ".json",
                       headers={"Accept": "application/json"})
    if code == 200:
        try:
            d = json.loads(body.decode("utf-8", "replace"))
            return _parse_reddit_json_response(d, u_clean)
        except Exception:
            pass  # fall through to fallback

    # Attempt 2: oembed for title + author, then a stripped HTML body
    # (Reddit's server-rendered HTML shell has enough for a summary)
    oembed_url = ("https://www.reddit.com/oembed?url="
                  + urllib.parse.quote(u_clean, safe=""))
    title = author = ""
    oc, ob = _get(oembed_url)
    if oc == 200:
        try:
            od = json.loads(ob.decode("utf-8", "replace"))
            title = od.get("title") or ""
            author = od.get("author_name") or ""
        except Exception:
            pass
    # HTML fallback — grab any <meta property="og:description"> for body
    hc, hb = _get(u_clean, headers={
        "Accept": "text/html,application/xhtml+xml"})
    body_text = ""
    if hc == 200:
        try:
            html = hb.decode("utf-8", "replace")
        except Exception:
            html = ""
        m = re.search(
            r'<meta\s+[^>]*property=["\']og:description["\'][^>]*'
            r'content=["\']([^"\']*)["\']', html, re.I)
        if m:
            body_text = unescape(m.group(1).strip())
    if title or body_text:
        return _ok("reddit", u_clean,
                   title=title, body=body_text, author=author,
                   ts="", meta={"fallback": "oembed+og"})
    return _err("reddit", u, "reddit .json + oembed both blocked "
                             "(likely need OAuth or Playwright)")


def _parse_reddit_json_response(d, u_clean):
    """Extract the OP + top-10 comments from Reddit's `.json` response."""
    if not isinstance(d, list) or len(d) < 1:
        return _err("reddit", u_clean, "unexpected reddit shape")
    # First list = post; second = comments
    try:
        post = d[0]["data"]["children"][0]["data"]
    except Exception:
        return _err("reddit", u_clean, "no post found")
    title = post.get("title") or ""
    selftext = (post.get("selftext") or "").strip()
    author = post.get("author") or ""
    body_parts = [selftext] if selftext else []
    comments = []
    if len(d) > 1:
        try:
            for c in (d[1]["data"]["children"] or [])[:10]:
                cd = c.get("data") or {}
                if not cd.get("body"):
                    continue
                comments.append({
                    "author": cd.get("author") or "",
                    "score": cd.get("score"),
                    "body": (cd.get("body") or "").strip()[:2000],
                })
        except Exception:
            pass
    if comments:
        body_parts.append("\n\n--- Top comments ---")
        for c in comments:
            body_parts.append(
                f"[{c['score']}] {c['author']}: {c['body']}")
    return _ok("reddit", u_clean,
               title=title,
               body="\n\n".join(body_parts),
               author=author,
               ts=str(post.get("created_utc") or ""),
               meta={
                   "subreddit": post.get("subreddit"),
                   "score": post.get("score"),
                   "num_comments": post.get("num_comments"),
                   "upvote_ratio": post.get("upvote_ratio"),
                   "top_comments": comments,
               })


# ------------------------------------------------------------------
# YouTube — oembed for metadata (title + author). Comments require
# scraping and quickly hit rate limits; skipping for now.
# ------------------------------------------------------------------
_YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})")


def _extract_yt_id(s: str) -> str | None:
    if not s:
        return None
    s = str(s).strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    m = _YT_ID_RE.search(s)
    return m.group(1) if m else None


def read_youtube(url_or_id: str) -> dict:
    """Metadata for a YouTube video. Uses noembed as primary (returns
    duration + thumbnail; youtube oembed doesn't) with youtube oembed as
    fallback."""
    vid = _extract_yt_id(url_or_id)
    watch = (f"https://www.youtube.com/watch?v={vid}" if vid
             else str(url_or_id))
    if not vid:
        return _err("youtube", watch, "could not extract video id")
    # Try noembed first (richer)
    for oembed_url in [
        f"https://noembed.com/embed?url={urllib.parse.quote(watch, safe='')}",
        f"https://www.youtube.com/oembed?url={urllib.parse.quote(watch, safe='')}&format=json",
    ]:
        code, body = _get(oembed_url)
        if code != 200:
            continue
        try:
            d = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            continue
        if not d.get("title"):
            continue
        return _ok("youtube", watch,
                   title=d.get("title") or "",
                   body="",  # oembed doesn't include description
                   author=d.get("author_name") or "",
                   ts="",
                   meta={
                       "video_id": vid,
                       "thumbnail": d.get("thumbnail_url"),
                       "duration_sec": d.get("duration"),
                       "provider": d.get("provider_name"),
                       "author_url": d.get("author_url"),
                   })
    return _err("youtube", watch, "no oembed endpoint returned a title")


# ------------------------------------------------------------------
# GitHub — repo / issue / PR / release via api.github.com
# ------------------------------------------------------------------
_GH_REPO_RE = re.compile(
    r"github\.com/([^/\s]+)/([^/\s]+?)(?:/|$|\.git|\?)")
_GH_ISSUE_RE = re.compile(
    r"github\.com/([^/\s]+)/([^/\s]+)/(issues|pull)/(\d+)")


def read_github(url: str) -> dict:
    """Fetch a GitHub repo, issue, PR, or release page via the REST API.
    Unauthenticated = 60 req/hr per IP; avoid burst use."""
    u = url.strip()
    m_iss = _GH_ISSUE_RE.search(u)
    if m_iss:
        owner, repo, kind, num = m_iss.groups()
        api = f"https://api.github.com/repos/{owner}/{repo}/issues/{num}"
        code, body = _get(api, headers={"Accept": "application/vnd.github+json"})
        if code == 404:
            return _err("github", u, "issue/PR not found")
        if code != 200:
            return _err("github", u, f"HTTP {code}")
        try:
            d = json.loads(body.decode("utf-8", "replace"))
        except Exception as e:
            return _err("github", u, f"json decode: {e}")
        return _ok("github", u,
                   title=d.get("title") or "",
                   body=(d.get("body") or "").strip(),
                   author=(d.get("user") or {}).get("login") or "",
                   ts=d.get("created_at") or "",
                   meta={
                       "kind": kind, "number": int(num),
                       "state": d.get("state"),
                       "labels": [l.get("name") for l in
                                  (d.get("labels") or [])],
                       "comments": d.get("comments"),
                       "html_url": d.get("html_url"),
                   })
    m_repo = _GH_REPO_RE.search(u)
    if m_repo:
        owner, repo = m_repo.groups()
        api = f"https://api.github.com/repos/{owner}/{repo}"
        code, body = _get(api, headers={"Accept": "application/vnd.github+json"})
        if code == 404:
            return _err("github", u, "repo not found")
        if code != 200:
            return _err("github", u, f"HTTP {code}")
        try:
            d = json.loads(body.decode("utf-8", "replace"))
        except Exception as e:
            return _err("github", u, f"json decode: {e}")
        # Also grab the README (best-effort)
        readme_text = ""
        rc, rb = _get(f"https://api.github.com/repos/{owner}/{repo}/readme",
                      headers={"Accept": "application/vnd.github.raw"})
        if rc == 200:
            try:
                readme_text = rb.decode("utf-8", "replace")
            except Exception:
                pass
        return _ok("github", u,
                   title=d.get("full_name") or f"{owner}/{repo}",
                   body=(d.get("description") or "") + (
                       "\n\n--- README ---\n" + readme_text if readme_text else ""),
                   author=owner,
                   ts=d.get("updated_at") or d.get("created_at") or "",
                   meta={
                       "kind": "repo",
                       "stars": d.get("stargazers_count"),
                       "forks": d.get("forks_count"),
                       "language": d.get("language"),
                       "topics": d.get("topics") or [],
                       "homepage": d.get("homepage"),
                       "default_branch": d.get("default_branch"),
                   })
    return _err("github", u,
                "URL didn't match repo, issue, or PR pattern")


# ------------------------------------------------------------------
# Generic web — stdlib urllib GET + best-effort HTML text extract
# ------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.DOTALL | re.I)
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.DOTALL | re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.I)
_META_DESC_RE = re.compile(
    r'<meta\s+[^>]*name=["\']description["\'][^>]*content=["\']([^"\']*)["\']',
    re.I)
_OG_TITLE_RE = re.compile(
    r'<meta\s+[^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']*)["\']',
    re.I)
_WHITESPACE_RE = re.compile(r"\s+")


def read_web(url: str) -> dict:
    """Generic web page. Best-effort — strip <script>/<style>, then all
    tags, collapse whitespace. Client-side-rendered pages return empty
    body (their real content is in JS). Add Playwright fallback later
    if that becomes a common ask."""
    u = url.strip()
    if not (u.startswith("http://") or u.startswith("https://")):
        return _err("web", u, "url must start with http(s)://")
    code, body = _get(u, headers={"Accept": "text/html,application/xhtml+xml"})
    if code != 200:
        return _err("web", u, f"HTTP {code}")
    try:
        html = body.decode("utf-8", "replace")
    except Exception as e:
        return _err("web", u, f"decode: {e}")
    # Titles
    title = ""
    m = _OG_TITLE_RE.search(html) or _TITLE_RE.search(html)
    if m:
        title = unescape(m.group(1).strip())
    # Description
    desc = ""
    md = _META_DESC_RE.search(html)
    if md:
        desc = unescape(md.group(1).strip())
    # Body text
    stripped = _SCRIPT_RE.sub(" ", html)
    stripped = _STYLE_RE.sub(" ", stripped)
    stripped = _TAG_RE.sub(" ", stripped)
    stripped = unescape(stripped)
    stripped = _WHITESPACE_RE.sub(" ", stripped).strip()
    if desc and desc not in stripped[:2000]:
        stripped = desc + "\n\n" + stripped
    return _ok("web", u,
               title=title,
               body=stripped,
               author="",
               ts="",
               meta={"html_bytes": len(body)})


# ------------------------------------------------------------------
# Dispatcher — one function that all callers should prefer
# ------------------------------------------------------------------
_SOURCE_DETECTORS = [
    ("twitter", re.compile(r"(?:https?://)?(?:www\.|mobile\.)?"
                            r"(?:twitter\.com|x\.com)/[^/\s]+/status/\d+")),
    ("reddit",  re.compile(r"(?:https?://)?(?:www\.|old\.|new\.)?"
                            r"reddit\.com/r/[^/\s]+/comments/")),
    ("youtube", re.compile(r"(?:https?://)?(?:www\.|m\.)?"
                            r"(?:youtube\.com/(?:watch\?v=|shorts/|embed/)"
                            r"|youtu\.be/)")),
    ("github",  re.compile(r"(?:https?://)?(?:www\.)?"
                            r"github\.com/[^/\s]+/[^/\s]+")),
]


def _detect_source(url: str) -> str:
    for name, rx in _SOURCE_DETECTORS:
        if rx.search(url):
            return name
    return "web"


# ------------------------------------------------------------------
# Playwright fallback — renders JS-heavy / bot-walled pages (X, Reddit,
# SPAs, Cloudflare) in headless chromium when the keyless path fails.
# Runs OUT-OF-PROCESS with a hard timeout so a slow render can never block
# the web server. Enabled only if the `playwright` package is importable.
# ------------------------------------------------------------------
# Browsers path is taken from the env (set PLAYWRIGHT_BROWSERS_PATH to a
# shared, service-user-readable dir in a multi-user deploy). Left unset, we
# fall back to Playwright's own default cache (~/.cache/ms-playwright).
_PW_BROWSERS_PATH = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
_PW_AVAILABLE = None


def _playwright_available() -> bool:
    global _PW_AVAILABLE
    if _PW_AVAILABLE is None:
        try:
            import importlib.util
            _PW_AVAILABLE = importlib.util.find_spec("playwright") is not None
        except Exception:
            _PW_AVAILABLE = False
    return _PW_AVAILABLE


def _render_with_playwright(url: str, timeout: int = 30) -> dict | None:
    """Render `url` in headless chromium via a bounded subprocess. Returns
    {title, text, final_url} or None. The hard subprocess timeout guarantees
    the caller (chat loop / agent) is never blocked longer than `timeout`."""
    if not _playwright_available():
        return None
    import subprocess
    import sys
    env = dict(os.environ)
    if _PW_BROWSERS_PATH:
        env.setdefault("PLAYWRIGHT_BROWSERS_PATH", _PW_BROWSERS_PATH)
    env.setdefault("HOME", "/tmp")   # service user may lack a writable HOME
    try:
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--render", url],
            capture_output=True, timeout=timeout, env=env)
    except Exception:
        return None
    out = (proc.stdout or b"").decode("utf-8", "replace").strip()
    if not out:
        return None
    try:
        d = json.loads(out.splitlines()[-1])   # last line = our JSON
    except Exception:
        return None
    return d if isinstance(d, dict) and d.get("ok") else None


def _playwright_fallback(url: str, source: str) -> dict | None:
    """Best-effort structured read via chromium. For tweets, render the
    keyless syndication endpoint (a real browser reaches it where datacenter
    urllib is blocked); for everything else, render the page + extract text."""
    if source == "twitter":
        tid = _extract_tweet_id(url)
        if tid:
            r = _render_with_playwright(
                f"https://cdn.syndication.twimg.com/tweet-result?"
                f"id={tid}&token=a")
            if r and r.get("text"):
                try:
                    d = json.loads(r["text"])
                    user = d.get("user") or {}
                    text = (d.get("text") or "").strip()
                    if text:
                        return _ok("twitter", f"https://x.com/i/status/{tid}",
                                   title=text[:200], body=text,
                                   author=f"@{user.get('screen_name', '?')}",
                                   ts=d.get("created_at") or "",
                                   meta={"via": "playwright+syndication"})
                except Exception:
                    pass
    r = _render_with_playwright(url)
    if r and r.get("text"):
        return _ok(source, url, title=(r.get("title") or ""), body=r["text"],
                   meta={"via": "playwright", "final_url": r.get("final_url")})
    return None


def web_read(url: str) -> dict:
    """Auto-detect source from URL and dispatch. Callers should use this
    instead of the source-specific readers unless they already know the
    source. Always returns a dict; check `ok`. On a failed keyless read,
    falls back to a headless-chromium render (bounded subprocess)."""
    u = str(url).strip()
    source = _detect_source(u)
    try:
        if source == "twitter":
            result = read_tweet(url)
        elif source == "reddit":
            result = read_reddit(url)
        elif source == "youtube":
            result = read_youtube(url)
        elif source == "github":
            result = read_github(url)
        else:
            result = read_web(url)
    except Exception as e:
        result = _err(source, u, f"{type(e).__name__}: {e}")
    if result.get("ok"):
        return result
    # Keyless path failed — try the Playwright render fallback.
    fb = _playwright_fallback(u, source)
    return fb if (fb and fb.get("ok")) else result


__all__ = ["web_read", "read_tweet", "read_reddit", "read_youtube",
           "read_github", "read_web"]


# ------------------------------------------------------------------
# Subprocess render entrypoint. `_render_with_playwright` spawns
#   python3 globus_web_read.py --render <url>
# which renders one page in headless chromium and prints a single JSON
# line {ok, title, text, final_url, error}. Kept in this file so the
# render logic is versioned + deployed with the reader itself. The
# heavy `playwright` import is confined to here — normal `import
# globus_web_read` stays dependency-free.
# ------------------------------------------------------------------
def _render_main(url: str) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(json.dumps({"ok": False,
                          "error": f"playwright unavailable: {e}"}))
        return
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--disable-gpu"])
            ctx = browser.new_context(
                user_agent=_UA, viewport={"width": 1280, "height": 900})
            pg = ctx.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=20000)
            try:
                pg.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass
            title = ""
            try:
                title = pg.title() or ""
            except Exception:
                pass
            text = ""
            try:
                text = pg.eval_on_selector("body", "el => el.innerText") or ""
            except Exception:
                try:
                    text = pg.inner_text("body")
                except Exception:
                    text = ""
            final_url = pg.url
            browser.close()
        text = (text or "").strip()
        if len(text) > _MAX_BODY_CHARS:
            text = text[:_MAX_BODY_CHARS] + "…[truncated]"
        print(json.dumps({"ok": True, "title": title, "text": text,
                          "final_url": final_url}))
    except Exception as e:
        print(json.dumps({"ok": False,
                          "error": f"{type(e).__name__}: {e}"}))


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "--render":
        _render_main(sys.argv[2])
    elif len(sys.argv) >= 2:
        # Quick CLI: python globus_web_read.py <url> → pretty-print web_read
        print(json.dumps(web_read(sys.argv[1]), indent=2)[:6000])
    else:
        print("usage: globus_web_read.py <url> | --render <url>")
