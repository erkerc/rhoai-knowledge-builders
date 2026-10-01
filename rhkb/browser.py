"""Headless browser, used only when plain HTTP is refused.

docs.redhat.com sits behind a CDN that sometimes rejects scripted clients with a
403 regardless of headers. Opening the site once in a real browser yields the
clearance cookies; copying those into the requests session is usually enough for
every subsequent fetch. If even that is refused, we can pull the page HTML
straight out of the browser.

Playwright is preferred; Selenium is used when Playwright is not installed.
Neither is a hard dependency - without them you get `--browser-fallback off`
behaviour and a clear message saying so.
"""

from __future__ import annotations

import time
from typing import Dict, Optional

from .util import LOG


class BrowserUnavailable(Exception):
    pass


class BrowserSession:
    """A single long-lived headless browser. Started lazily, reused for the run."""

    def __init__(self, engine: str = "auto") -> None:
        self.engine = engine
        self.backend: Optional[str] = None
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._driver = None
        self._started = False

    # -- lifecycle --------------------------------------------------------
    def available(self) -> bool:
        return bool(self._pick())

    def _pick(self) -> Optional[str]:
        order = ["playwright", "selenium"] if self.engine == "auto" else [self.engine]
        for name in order:
            try:
                if name == "playwright":
                    import playwright.sync_api  # noqa: F401
                    return "playwright"
                if name == "selenium":
                    import selenium  # noqa: F401
                    return "selenium"
            except ImportError:
                continue
        return None

    def start(self) -> None:
        if self._started:
            return
        backend = self._pick()
        if not backend:
            raise BrowserUnavailable(
                "no headless browser available - install one:\n"
                "  pip install playwright && playwright install chromium\n"
                "  or: pip install selenium  (with Chrome installed)"
            )
        if backend == "playwright":
            self._start_playwright()
        else:
            self._start_selenium()
        self.backend = backend
        self._started = True

    def _start_playwright(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        except Exception as exc:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
            if "executable doesn't exist" in str(exc).lower() or "playwright install" in str(exc).lower():
                raise BrowserUnavailable(
                    "the playwright package is installed but its Chromium build is not - "
                    "run once:  python -m playwright install chromium"
                ) from exc
            raise
        self._context = self._browser.new_context(viewport={"width": 1280, "height": 1600})
        self._page = self._context.new_page()

    def _start_selenium(self) -> None:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1280,1600")
        try:
            self._driver = webdriver.Chrome(options=options)
        except Exception as exc:
            raise BrowserUnavailable(f"could not start headless Chrome: {exc}") from exc
        self._driver.set_page_load_timeout(90)

    def stop(self) -> None:
        for closer in (self._context, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        try:
            if self._driver:
                self._driver.quit()
        except Exception:
            pass
        self._started = False

    # -- use --------------------------------------------------------------
    def visit(self, url: str, wait: float = 2.5) -> None:
        self.start()
        if self.backend == "playwright":
            self._page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        else:
            self._driver.get(url)
        time.sleep(wait)  # let any interstitial challenge resolve

    def html(self, url: str, wait: float = 2.5) -> str:
        self.visit(url, wait=wait)
        return self._page.content() if self.backend == "playwright" else self._driver.page_source

    def cookies(self) -> Dict[str, str]:
        self.start()
        if self.backend == "playwright":
            return {c["name"]: c["value"] for c in self._context.cookies()}
        return {c["name"]: c["value"] for c in self._driver.get_cookies()}

    def user_agent(self) -> str:
        self.start()
        if self.backend == "playwright":
            return self._page.evaluate("() => navigator.userAgent")
        return self._driver.execute_script("return navigator.userAgent")

    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, *exc) -> bool:
        self.stop()
        return False


def warm_up(sess, url: str, engine: str = "auto") -> Optional[BrowserSession]:
    """Open `url` in a browser and copy its cookies + UA into `sess`.

    Returns the live browser (worth keeping: some pages stay refused over plain
    HTTP and have to be read out of the browser), or None when none is available.
    """
    browser = BrowserSession(engine=engine)
    if not browser.available():
        LOG.warning("request refused and no headless browser installed - "
                    "pip install playwright && playwright install chromium")
        return None
    try:
        LOG.info("request refused; warming up in a headless browser")
        browser.visit(url)
        cookies = browser.cookies()
        if cookies:
            sess.cookies.update(cookies)
        agent = browser.user_agent()
        if agent:
            sess.headers["User-Agent"] = agent
        LOG.info("picked up %d cookie(s) via %s", len(cookies), browser.backend)
        return browser
    except Exception as exc:
        LOG.warning("browser warm-up failed: %s", exc)
        browser.stop()
        return None
