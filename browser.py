"""
JARVIS Browser — Playwright-based web browsing capabilities.

Provides search, page visits, screenshots, and multi-step research.
Runs a visible Chromium instance (headless configurable via BROWSER_HEADLESS env var).

Windows-compatible: handles console window suppression, file handle locking,
and uses a singleton pattern with an asyncio lock to prevent race conditions.

Integration pattern:
- Simple URL opens (fire-and-forget) → actions.py open_browser()
- Scraping / research / screenshots → this module
- Research report writing → server.py _execute_research() consumes ResearchResult
"""

import asyncio
import logging
import os
import platform
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

log = logging.getLogger("jarvis.browser")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_OS = platform.system()
HEADLESS = os.getenv("BROWSER_HEADLESS", "false").lower() == "true"
TIMEOUT_MS = 30_000

# Realistic user agents per platform
_USER_AGENTS = {
    "Windows": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Darwin": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Linux": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}
USER_AGENT = _USER_AGENTS.get(_OS, _USER_AGENTS["Linux"])


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PageContent:
    title: str
    url: str
    text_content: str
    word_count: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ResearchResult:
    """
    Structured research output consumed by server.py _execute_research().

    - topic: original search query
    - sources: list of URLs visited
    - pages: full PageContent for each visited page (for report writing)
    - summary: concatenated text for quick LLM consumption
    - key_findings: top result titles (for voice summary)
    """
    topic: str
    sources: list[str]
    pages: list[PageContent]
    summary: str
    key_findings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pages"] = [p.to_dict() for p in self.pages]
        return d

    def to_prompt_context(self, max_chars_per_page: int = 3000) -> str:
        """Format research data as a prompt section for the report writer."""
        parts = [f"# Research: {self.topic}\n"]
        for i, page in enumerate(self.pages, 1):
            parts.append(f"## Source {i}: {page.title}")
            parts.append(f"URL: {page.url}")
            parts.append(page.text_content[:max_chars_per_page])
            parts.append("")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Browser Manager (Singleton)
# ---------------------------------------------------------------------------

class JarvisBrowser:
    """
    Playwright-based browser automation for JARVIS.

    Singleton-safe via asyncio.Lock — only one browser instance is created
    regardless of concurrent callers. Safe to share across the FastAPI app
    by attaching to app.state in the lifespan context.

    Usage:
        # In lifespan:
        app.state.browser = JarvisBrowser()
        yield
        await app.state.browser.close()

        # In handlers:
        result = await request.app.state.browser.research("Python FastAPI tutorial")
    """

    def __init__(self):
        self._pw = None
        self._browser = None
        self._context = None
        self._lock = asyncio.Lock()

    # -- Lifecycle ------------------------------------------------------------

    async def _ensure_browser(self):
        """Launch browser if not already running. Thread-safe via lock."""
        if self._browser and self._context:
            return

        async with self._lock:
            # Double-check after acquiring lock
            if self._browser and self._context:
                return

            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()

            # On Windows, args suppress the console window flash
            launch_args = []
            if _OS == "Windows":
                launch_args = ["--disable-extensions", "--no-sandbox"]

            self._browser = await self._pw.chromium.launch(
                headless=HEADLESS,
                args=launch_args if launch_args else None,
            )
            self._context = await self._browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
            )
            log.info(f"Browser launched ({'headless' if HEADLESS else 'visible'} Chromium, {_OS})")

    async def close(self):
        """Shut down the browser. Safe to call multiple times."""
        async with self._lock:
            try:
                if self._context:
                    await self._context.close()
                if self._browser:
                    await self._browser.close()
                if self._pw:
                    await self._pw.stop()
                log.info("Browser closed")
            except Exception as e:
                log.warning(f"Browser close error (non-fatal): {e}")
            finally:
                self._pw = None
                self._browser = None
                self._context = None

    @property
    def is_running(self) -> bool:
        return self._browser is not None and self._context is not None

    # -- Internal helpers -----------------------------------------------------

    async def _new_page(self):
        """Open a new page in the shared browser context."""
        await self._ensure_browser()
        assert self._context is not None
        return await self._context.new_page()

    async def _safe_goto(self, page, url: str) -> bool:
        """Navigate to URL, returning True on success."""
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            return True
        except Exception as e:
            log.warning(f"Navigation failed for '{url}': {e}")
            return False

    @staticmethod
    async def _extract_text(page) -> dict:
        """Extract title and main text from the current page."""
        return await page.evaluate("""
            () => {
                const title = document.title || '';
                const main = (
                    document.querySelector('main') ||
                    document.querySelector('article') ||
                    document.querySelector('[role="main"]') ||
                    document.body
                );
                const clone = main.cloneNode(true);
                const remove = clone.querySelectorAll(
                    'script, style, nav, header, footer, aside, ' +
                    '.sidebar, .menu, .ad, .advertisement, iframe, ' +
                    '.cookie-banner, .popup, [aria-hidden="true"]'
                );
                remove.forEach(el => el.remove());
                const text = (clone.innerText || clone.textContent || '').trim();
                return { title, text: text.substring(0, 6000) };
            }
        """)

    # -- Public API -----------------------------------------------------------

    async def search(self, query: str) -> list[SearchResult]:
        """
        Search DuckDuckGo and return top results.

        Returns an empty list (not an error) if DDG blocks the request —
        callers should handle this gracefully.
        """
        if not query:
            return []

        page = await self._new_page()
        results = []

        try:
            ok = await self._safe_goto(
                page,
                f"https://html.duckduckgo.com/html/?q={query}",
            )
            if not ok:
                return []

            raw = await page.evaluate("""
                () => {
                    const items = document.querySelectorAll('.result');
                    return Array.from(items).slice(0, 6).map(item => ({
                        title: (
                            item.querySelector('.result__title a') ||
                            item.querySelector('.result__a')
                        )?.textContent?.trim() || '',
                        url: (
                            item.querySelector('.result__title a') ||
                            item.querySelector('.result__a')
                        )?.href || '',
                        snippet: item.querySelector('.result__snippet')?.textContent?.trim() || ''
                    })).filter(r => r.title && r.url);
                }
            """)

            results = [
                SearchResult(title=r["title"], url=r["url"], snippet=r.get("snippet", ""))
                for r in raw
            ]
            log.info(f"Search '{query}' → {len(results)} results")

            # Let user see results briefly if visible
            if not HEADLESS and results:
                await asyncio.sleep(1.5)

        except Exception as e:
            log.warning(f"Search failed: {e}")
        finally:
            await page.close()

        return results

    async def visit(self, url: str) -> PageContent:
        """
        Visit a URL and extract clean main text content.

        Always returns a PageContent — on failure, text_content contains
        the error message so callers don't need to handle None.
        """
        page = await self._new_page()

        try:
            ok = await self._safe_goto(page, url)
            if not ok:
                return PageContent(
                    title="Error",
                    url=url,
                    text_content=f"Failed to load: {url}",
                    word_count=0,
                )

            data = await self._extract_text(page)

            if not HEADLESS:
                await asyncio.sleep(2)

            text = data.get("text", "")
            return PageContent(
                title=data.get("title", ""),
                url=url,
                text_content=text,
                word_count=len(text.split()),
            )

        except Exception as e:
            log.warning(f"Visit failed for '{url}': {e}")
            return PageContent(
                title="Error",
                url=url,
                text_content=f"Error visiting page: {e}",
                word_count=0,
            )
        finally:
            await page.close()

    async def screenshot(self, url: str, path: Optional[str] = None) -> str:
        """
        Take a full-page screenshot of a URL.

        Returns the file path to the saved PNG, or empty string on failure.
        Windows note: file handle is closed before Playwright writes to avoid
        the 'file in use' error that occurs with NamedTemporaryFile on Windows.
        """
        # Create temp file path without keeping the handle open (Windows-safe)
        if not path:
            tmp = tempfile.NamedTemporaryFile(
                suffix=".png", prefix="jarvis_ss_", delete=False
            )
            path = tmp.name
            tmp.close()  # Close immediately — Playwright will write to the path

        page = await self._new_page()

        try:
            ok = await self._safe_goto(page, url)
            if not ok:
                return ""

            await page.wait_for_timeout(1000)
            await page.screenshot(path=path, full_page=True)
            log.info(f"Screenshot saved: {path}")
            return path

        except Exception as e:
            log.warning(f"Screenshot failed for '{url}': {e}")
            # Clean up empty file on failure
            try:
                p = Path(path)
                if p.exists() and p.stat().st_size == 0:
                    p.unlink()
            except Exception:
                pass
            return ""

        finally:
            await page.close()

    async def research(self, topic: str, max_sources: int = 3) -> ResearchResult:
        """
        Multi-step research: search → visit top results → return structured data.

        The ResearchResult is designed to be consumed by server.py
        _execute_research(), which passes it to the Gemini/claude-p report writer.

        Args:
            topic: The research query.
            max_sources: How many pages to visit (default 3 to stay fast).

        Returns:
            ResearchResult with full page content and structured metadata.
        """
        search_results = await self.search(topic)

        if not search_results:
            log.warning(f"No search results for '{topic}'")
            return ResearchResult(
                topic=topic,
                sources=[],
                pages=[],
                summary="No search results found.",
                key_findings=[],
            )

        sources: list[str] = []
        pages: list[PageContent] = []

        for result in search_results[:max_sources]:
            try:
                page_content = await self.visit(result.url)
                # Skip error pages and near-empty pages
                if page_content.word_count < 50:
                    log.debug(f"Skipping low-content page: {result.url}")
                    continue
                sources.append(result.url)
                pages.append(page_content)
            except Exception as e:
                log.warning(f"Failed to visit {result.url}: {e}")
                continue

        # Build concatenated summary for quick LLM consumption
        summary_parts = []
        for page in pages:
            summary_parts.append(
                f"[{page.title}]\n{page.text_content[:1500]}"
            )
        summary = "\n\n---\n\n".join(summary_parts) if summary_parts else "No content retrieved."

        return ResearchResult(
            topic=topic,
            sources=sources,
            pages=pages,
            summary=summary,
            key_findings=[r.title for r in search_results[:max_sources]],
        )