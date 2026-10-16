"""SSO auto-login driver (optional, requires `pip install mcp-plugin-gen[sso]` + `playwright install chromium`).

Drives the authorization URL through a headless browser and fills the SSO form step by step from
config (selectors + values), e.g. mobile → ID/Iqama → OTP → consent. The redirect to `redirect_uri`
is intercepted inside the browser, so the authorization code is captured even when the redirect
host is not routable from this machine — no listening socket is required.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse, parse_qs

from .config import SsoConfig


class SsoError(RuntimeError):
    pass


def _query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}


SUBMIT_FALLBACK = ("#kc-login, input[type='submit'], button[type='submit'], "
                   "button:has-text('Login'), button:has-text('Sign in'), button:has-text('Continue'), "
                   "button:has-text('Next'), button:has-text('Verify'), button:has-text('Submit'), "
                   "button:has-text('دخول'), button:has-text('تسجيل'), button:has-text('التالي'), "
                   "button:has-text('متابعة'), button:has-text('تحقق'), button:has-text('إرسال')")


def dump_form(page, path: Path, log) -> None:
    """Write every input/button/select/a[role=button] on the page (and inside frames) with its attributes,
    so a failing selector can be fixed from the dump without a GUI."""
    js = """() => [...document.querySelectorAll('input,button,select,textarea,[role=button],form')].map(e => {
        const a = {}; for (const x of e.attributes) a[x.name] = x.value;
        return e.tagName.toLowerCase() + ' ' + JSON.stringify(a) + (e.innerText ? '  text=' + e.innerText.trim().slice(0,60) : '');
    }).join('\n')"""
    out = [f"url: {page.url}", f"title: {page.title()}", ""]
    for fr in page.frames:
        try:
            out.append(f"--- frame: {fr.url}")
            out.append(fr.evaluate(js))
        except Exception as e:  # noqa: BLE001
            out.append(f"(frame not readable: {e})")
    path.write_text("\n".join(out), encoding="utf-8")
    log(f"[sso] form elements dumped to {path}")


def make_sso_opener(sso: SsoConfig, redirect_uri: str, log: Callable[[str], None] = print,
                    page_hook: Optional[Callable] = None) -> Callable[[str], Optional[dict]]:
    """`page_hook(page)` is called right after the page is created (used by tests to stub the IdP)."""
    try:
        from playwright.sync_api import sync_playwright  # noqa
    except ImportError as e:  # pragma: no cover
        raise SsoError("Playwright not installed: pip install 'mcp-plugin-gen[sso]' && playwright install chromium") from e

    ru = urlparse(redirect_uri)
    redirect_prefix = f"{ru.scheme}://{ru.netloc}{ru.path or '/'}"

    def opener(auth_url: str) -> Optional[dict]:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
        shots = Path(sso.screenshot_dir) if sso.screenshot_dir else None
        if shots:
            shots.mkdir(parents=True, exist_ok=True)
        captured: dict = {}

        with sync_playwright() as p:
            launch_kw = {"headless": sso.headless, "slow_mo": sso.slow_mo_ms}
            proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or os.environ.get("HTTP_PROXY")
            if proxy:  # corporate proxy: use it, but never for loopback / NO_PROXY hosts
                bypass = ",".join(filter(None, ["127.0.0.1", "localhost", os.environ.get("NO_PROXY", os.environ.get("no_proxy", ""))]))
                launch_kw["proxy"] = {"server": proxy, "bypass": bypass}
            browser = getattr(p, sso.browser).launch(**launch_kw)
            page = browser.new_page()
            page.set_default_timeout(sso.step_timeout_ms)
            if page_hook:
                page_hook(page)

            # Capture the redirect to redirect_uri however it happens:
            #  - route: for JS/meta redirects (interceptable) → answer locally
            #  - response Location header / redirected request: for HTTP 302s (Chromium does not route those)
            # The callback host never needs to be reachable from this machine.
            def _capture(url: str, how: str):
                if url.startswith(redirect_prefix) and not captured:
                    captured.update(_query(url))
                    log(f"[sso] captured redirect via {how} (code {'present' if 'code' in captured else 'MISSING'})")

            def on_route(route, request):
                _capture(request.url, "route")
                route.fulfill(status=200, content_type="text/html",
                              body="<html><body style='font-family:sans-serif'><h3>Login complete.</h3></body></html>")
            page.route(f"{redirect_prefix}*", on_route)
            page.on("request", lambda r: _capture(r.url, "request"))
            page.on("requestfailed", lambda r: _capture(r.url, "requestfailed"))

            def on_response(r):
                if 300 <= r.status < 400:
                    loc = r.headers.get("location", "")
                    if loc.startswith("/"):
                        u = urlparse(r.url); loc = f"{u.scheme}://{u.netloc}{loc}"
                    _capture(loc, "302 Location")
            page.on("response", on_response)

            def nav(fn, *a, **kw):
                """Run a navigation-triggering action; a network error on the (unreachable) callback host
                is expected once the code is captured."""
                try:
                    return fn(*a, **kw)
                except Exception as e:  # noqa: BLE001
                    if captured:
                        log(f"[sso] navigation to callback host failed as expected after capture ({type(e).__name__})")
                        return None
                    raise

            log(f"[sso] opening authorization URL ({len(sso.steps)} configured steps)")
            nav(page.goto, auth_url)
            if shots and not captured:
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                try:
                    page.screenshot(path=str(shots / "00-login-page.png"), full_page=True)
                    dump_form(page, shots / "00-login-page-elements.txt", log)
                except Exception:
                    pass
            for i, step in enumerate(sso.steps, 1):
                if captured:
                    log("[sso] redirect already captured; skipping remaining steps")
                    break
                label = step.get("name", f"step{i}")
                optional = bool(step.get("optional", False))
                try:
                    if step.get("wait_for"):
                        page.wait_for_selector(step["wait_for"])
                    if step.get("click"):
                        nav(page.click, step["click"])
                    if step.get("fill"):
                        page.fill(step["fill"], str(step.get("value", "")))
                    if step.get("type"):               # OTP boxes that need key events
                        page.type(step["type"], str(step.get("value", "")), delay=step.get("delay_ms", 50))
                    if step.get("select"):
                        page.select_option(step["select"], str(step.get("value", "")))
                    if step.get("submit"):
                        sel = step["submit"]
                        try:
                            nav(page.click, sel, timeout=min(8000, sso.step_timeout_ms))
                        except PwTimeout:
                            try:
                                log(f"[sso] {label}: submit selector not found, trying generic submit buttons")
                                nav(page.click, SUBMIT_FALLBACK, timeout=5000)
                            except PwTimeout:
                                target = step.get("fill") or step.get("type")
                                log(f"[sso] {label}: no submit button found, pressing Enter")
                                if target:
                                    nav(page.press, target, "Enter")
                                else:
                                    nav(page.keyboard.press, "Enter")
                        if step.get("settle_ms", 1500):
                            page.wait_for_timeout(int(step.get("settle_ms", 1500)))
                    if step.get("press"):
                        nav(page.keyboard.press, step["press"])
                    if step.get("wait_ms"):
                        page.wait_for_timeout(int(step["wait_ms"]))
                    if shots and not captured:
                        try:
                            page.screenshot(path=str(shots / f"{i:02d}-{label}.png"), full_page=True)
                            dump_form(page, shots / f"{i:02d}-{label}-elements.txt", log)
                        except Exception:
                            pass
                    log(f"[sso] {label} ok  (url: {page.url[:90]})")
                except PwTimeout as e:
                    if shots:
                        try:
                            page.screenshot(path=str(shots / f"{i:02d}-{label}-FAILED.png"), full_page=True)
                            dump_form(page, shots / f"{i:02d}-{label}-FAILED-elements.txt", log)
                        except Exception:
                            pass
                    if optional:
                        log(f"[sso] {label} skipped (optional, selector not found)")
                        continue
                    browser.close()
                    raise SsoError(f"SSO step '{label}' timed out: {e}. Check selectors in sso.steps "
                                   f"(screenshots in {shots or 'sso.screenshot_dir'}).") from e

            if not captured:
                try:
                    page.wait_for_url(lambda u: u.startswith(redirect_prefix), timeout=sso.step_timeout_ms)
                except Exception:
                    pass
                _capture(page.url, "final url")
            if not captured:
                if shots:
                    try:
                        page.screenshot(path=str(shots / "99-no-redirect.png"))
                    except Exception:
                        pass
                log(f"[sso] WARNING: no redirect to {redirect_prefix} observed; final url: {page.url[:120]}")
            page.wait_for_timeout(300)
            browser.close()
        return captured or None

    return opener
