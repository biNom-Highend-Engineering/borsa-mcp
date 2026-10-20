"""
Dovizcom Authentication Manager
This module handles dynamic token extraction and management for Doviz.com API.

How doviz.com's own pages authenticate to api.doviz.com (verified 2026-10-20):

- Every page sets `$.ajaxSetup({headers: {"Authorization": "Bearer " + (function(){...})()}})`.
  The token is no longer a plain string in the HTML: it is produced by an inline
  obfuscated function whose last call carries the encoded token, e.g.
  `...})("AAwkCAhk...",73,"hwAJCkpxK",8,5,47)})()`. The decoder, the alphabet and
  the offsets change per page render, so they are parsed, not hardcoded.
- The token rotates with each page render (pages are CDN-cached for ~2 minutes) but
  stays valid for a while after it stops being served, so caching it is fine as long
  as a 401 forces a re-scrape.
- api.doviz.com also requires `X-Requested-With: XMLHttpRequest` (401 without it) and
  an `Origin` header (403 without it); the provider sets both.

The hardcoded fallback tokens this module used to carry are all rejected now, and
sending a dead token turned a scraping failure into an opaque `HTTP 401
{"error":true,"message":""}`. When no token can be obtained we raise instead.
"""
import asyncio
import httpx
import logging
import time
import re
from typing import Optional

logger = logging.getLogger(__name__)


class DovizcomAuthError(Exception):
    """No usable api.doviz.com token could be obtained."""


# The obfuscated token expression that follows `"Authorization": "Bearer " +`. Its
# final call is `(encoded, <unused>, alphabet, offset, base, <unused>)`.
_OBFUSCATED_TOKEN_RE = re.compile(
    r'Bearer\s*"\s*\+\s*\(function\(\)\{.*?\}\)\('
    r'"(?P<encoded>[^"]+)"\s*,\s*\d+\s*,\s*"(?P<alphabet>[^"]+)"\s*,\s*'
    r'(?P<offset>\d+)\s*,\s*(?P<base>\d+)\s*,\s*\d+\s*\)\s*\}\)\(\)',
    re.DOTALL,
)

# A token written out literally, in case doviz.com drops the obfuscation again.
_LITERAL_TOKEN_RE = re.compile(
    r'Bearer\s+([a-f0-9]{64})\b|BaseAPI\.init\(\s*["\']([a-f0-9]{64})["\']',
    re.IGNORECASE,
)

_TOKEN_SHAPE_RE = re.compile(r'^[A-Za-z0-9._~+/=-]{16,512}$')


def decode_obfuscated_token(encoded: str, alphabet: str, offset: int, base: int) -> str:
    """Python port of doviz.com's inline token decoder.

    `encoded` is a run of segments, each terminated by `alphabet[base]`. Inside a
    segment, the character `alphabet[j]` stands for digit j; the segment read in
    `base`, minus `offset`, is one character code of the token.
    """
    if not 2 <= base < len(alphabet):
        raise ValueError(f"invalid base {base} for alphabet of length {len(alphabet)}")
    delimiter = alphabet[base]
    segments = encoded.split(delimiter)
    if segments[-1] != "":
        raise ValueError("encoded token does not end with a delimiter")

    chars = []
    for segment in segments[:-1]:
        # Same order as the JS: replace alphabet[0], then alphabet[1], ...
        for digit, symbol in enumerate(alphabet):
            segment = segment.replace(symbol, str(digit))
        chars.append(chr(int(segment, base) - offset))
    # JS: decodeURIComponent(escape(r)) — reinterpret the code points as UTF-8 bytes.
    return "".join(chars).encode("latin-1").decode("utf-8")


def extract_token_from_html(html: str) -> Optional[str]:
    """Pull the api.doviz.com bearer token out of a doviz.com page, or None."""
    match = _OBFUSCATED_TOKEN_RE.search(html)
    if match:
        try:
            token = decode_obfuscated_token(
                match.group("encoded"),
                match.group("alphabet"),
                int(match.group("offset")),
                int(match.group("base")),
            )
        except (ValueError, UnicodeError) as e:
            logger.warning(f"Found obfuscated doviz.com token but could not decode it: {e}")
        else:
            if _TOKEN_SHAPE_RE.match(token):
                return token
            logger.warning("Decoded doviz.com token has an unexpected shape; ignoring it")

    match = _LITERAL_TOKEN_RE.search(html)
    if match:
        return match.group(1) or match.group(2)
    return None


class DovizcomAuthManager:
    """Manages authentication tokens for Doviz.com API with automatic refresh."""

    TOKEN_SOURCE_URL = "https://www.doviz.com/"
    TOKEN_EXPIRY_SECONDS = 600  # tokens rotate per page render; a 401 also forces a refresh
    RETRY_COOLDOWN_SECONDS = 60  # don't hammer doviz.com after a failed scrape

    def __init__(self, client: httpx.AsyncClient):
        self._http_client = client
        self._current_token: Optional[str] = None
        self._token_expiry: Optional[float] = None
        self._last_failure: Optional[float] = None
        self._last_error: Optional[str] = None
        self._lock = asyncio.Lock()

    async def get_valid_token(self) -> str:
        """
        Get a valid Bearer token for API requests.

        Returns:
            Valid Bearer token string

        Raises:
            DovizcomAuthError: if no token could be scraped from doviz.com.
        """
        if self._is_token_valid():
            return self._current_token

        async with self._lock:
            # Another task may have refreshed while we waited for the lock.
            if self._is_token_valid():
                return self._current_token

            if self._last_failure and time.time() - self._last_failure < self.RETRY_COOLDOWN_SECONDS:
                raise DovizcomAuthError(
                    f"No doviz.com API token available (last attempt failed: {self._last_error})"
                )

            try:
                token = await self._extract_token_from_website()
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                token = None
            else:
                if not token:
                    self._last_error = (
                        f"no bearer token found on {self.TOKEN_SOURCE_URL}; "
                        "doviz.com may have changed how it embeds it"
                    )

            if not token:
                self._last_failure = time.time()
                logger.warning(f"doviz.com token extraction failed: {self._last_error}")
                raise DovizcomAuthError(f"Could not obtain a doviz.com API token: {self._last_error}")

            self._current_token = token
            self._token_expiry = time.time() + self.TOKEN_EXPIRY_SECONDS
            self._last_failure = None
            self._last_error = None
            logger.info("Successfully extracted fresh token from doviz.com")
            return token

    async def refresh_token_on_401(self, rejected_token: Optional[str] = None) -> str:
        """
        Force token refresh when getting 401 errors.

        Args:
            rejected_token: the token the API just rejected. If another task has
                already replaced it, the newer token is returned without re-scraping.

        Returns:
            New Bearer token string
        """
        logger.warning("Received 401 error, forcing token refresh")
        if rejected_token is None or rejected_token == self._current_token:
            self._current_token = None
            self._token_expiry = None
        return await self.get_valid_token()

    def _is_token_valid(self) -> bool:
        """Check if current token is still valid."""
        return (self._current_token is not None and
                self._token_expiry is not None and
                time.time() < self._token_expiry)

    async def _extract_token_from_website(self) -> Optional[str]:
        """
        Extract Bearer token from doviz.com website.

        Returns:
            Extracted token string or None if the page carries no recognisable token.
        """
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7',
        }
        response = await self._http_client.get(
            self.TOKEN_SOURCE_URL,
            headers=headers,
            timeout=30.0,
            follow_redirects=True,
        )
        response.raise_for_status()
        return extract_token_from_html(response.text)
