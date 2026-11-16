#!/usr/bin/env python3
"""Accessibility check of the page header (navigation and theme button).

Runs axe-core on the header in every color mode at desktop and phone
width, then checks the theme button by keyboard: its name, the tooltip on
focus, the focus ring, tooltip contrast, and that Enter steps the theme.
It changes the saved theme while it runs and puts it back to Auto.

Needs Playwright for Python with its Chromium, and a local copy of
axe-core (npm install axe-core). It only reads from the page it is given.

Usage: header_a11y.py URL AXE_MIN_JS
Exit code: 0 when every check passed, 1 otherwise.
"""

import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

VIEWPORTS = {"desktop": (1024, 768), "phone": (375, 812)}


def luminance(color: str) -> float:
    """Relative luminance of a CSS rgb()/rgba() color (alpha ignored)."""
    parts = [int(x) / 255 for x in re.findall(r"\d+", color)[:3]]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
              for c in parts]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(first: str, second: str) -> float:
    """WCAG contrast ratio of two opaque colors."""
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def set_theme(page, base: str, theme: str) -> None:
    """Save a theme by posting the page's own form values."""
    token = page.locator("form.theme input[name=token]").get_attribute(
        "value")
    page.request.post(base + "/theme", form={
        "token": token, "theme": theme, "back": "/"})


def axe_header(page, axe: str) -> list:
    """axe-core violations inside the header (WCAG 2.x A/AA rules)."""
    page.add_script_tag(content=axe)
    result = page.evaluate("""async () => (await axe.run('header', {
        runOnly: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa',
                  'best-practice']})).violations.map(v =>
        v.id + ': ' + v.nodes.length + ' node(s) - ' + v.help)""")
    return result


def main() -> int:
    """Run every check and print one line per result."""
    base, axe = sys.argv[1].rstrip("/"), Path(sys.argv[2]).read_text()
    failures = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for scheme in ("light", "dark"):
            for theme in ("auto", "light", "dark"):
                for name, (width, height) in VIEWPORTS.items():
                    context = browser.new_context(
                        bypass_csp=True, color_scheme=scheme,
                        viewport={"width": width, "height": height})
                    page = context.new_page()
                    page.goto(base + "/")
                    set_theme(page, base, theme)
                    page.goto(base + "/tags")
                    found = axe_header(page, axe)
                    wide = page.evaluate(
                        "document.documentElement.scrollWidth > "
                        "document.documentElement.clientWidth")
                    label = f"system={scheme} theme={theme} {name}"
                    print(f"axe {label}: {found or 'no violations'}")
                    failures += [f"{label} {item}" for item in found]
                    if wide:
                        failures.append(f"{label}: page scrolls sideways")
                    context.close()

        # Keyboard checks on the theme button, in both forced themes.
        for theme in ("light", "dark"):
            context = browser.new_context(bypass_csp=True)
            page = context.new_page()
            page.goto(base + "/")
            set_theme(page, base, theme)
            page.goto(base + "/")
            button = page.locator("form.theme button")
            for _ in range(20):
                page.keyboard.press("Tab")
                if button.evaluate("b => b === document.activeElement"):
                    break
            else:
                failures.append(f"{theme}: theme button not reachable by Tab")
            snapshot = page.accessibility.snapshot(root=button.element_handle())
            tip = page.locator("form.theme .tip")
            styles = page.evaluate("""() => {
                const b = document.querySelector('form.theme button');
                const t = document.querySelector('form.theme .tip');
                const h = document.querySelector('header');
                const s = getComputedStyle(b), ts = getComputedStyle(t);
                return {outline: s.outlineStyle + ' ' + s.outlineWidth,
                        ring: s.outlineColor,
                        header: getComputedStyle(h).backgroundColor,
                        tipFg: ts.color, tipBg: ts.backgroundColor,
                        icon: getComputedStyle(b.querySelector(
                            '.theme-icon')).borderTopColor,
                        size: [b.offsetWidth, b.offsetHeight]};}""")
            checks = {
                "name": snapshot and snapshot.get("role") == "button"
                and snapshot.get("name", "").startswith("Theme: "),
                "tooltip shows on focus": tip.is_visible(),
                "focus ring": styles["outline"].startswith("solid"),
                "focus ring contrast >= 3": contrast(
                    styles["ring"], styles["header"]) >= 3,
                "icon outline contrast >= 3": contrast(
                    styles["icon"], styles["header"]) >= 3,
                "tooltip text contrast >= 4.5": contrast(
                    styles["tipFg"], styles["tipBg"]) >= 4.5,
                "target at least 24x24": min(styles["size"]) >= 24,
            }
            before = page.locator("html").get_attribute("data-theme")
            page.keyboard.press("Enter")
            page.wait_for_load_state()
            after = page.locator("html").get_attribute("data-theme")
            checks["Enter steps the theme"] = before != after
            print(f"keyboard theme={theme}: name={snapshot and snapshot.get('name')!r}")
            for check, passed in checks.items():
                print(f"  {'ok  ' if passed else 'FAIL'} {check}")
                if not passed:
                    failures.append(f"keyboard theme={theme}: {check}")
            context.close()

        context = browser.new_context(bypass_csp=True)
        page = context.new_page()
        page.goto(base + "/")
        set_theme(page, base, "auto")
        context.close()
        browser.close()
    print("\n".join(["", "FAILURES:"] + failures) if failures
          else "\nAll header checks passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
