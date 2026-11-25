"""Mobile layout invariants for the template set.

These encode the defects found in the mobile audit so they cannot come back:

* a full page without a viewport meta renders zoomed-out on a phone;
* a wide table inside a clipping card loses columns with no way to scroll;
* a form control under 16px makes iOS Safari zoom the page on focus.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase


BASE = Path(settings.BASE_DIR)
TEMPLATES = BASE / "templates"
STATIC_CSS = BASE / "static" / "css"

def _is_full_page(text: str) -> bool:
    """Partials have no <html> element; only full documents need a viewport."""
    return "<html" in text.lower()


def _pages():
    """Every full HTML document, including the ones delivered by email.

    Mail is read on phones more than anywhere else, so the email templates are
    held to the same standard as the site itself.
    """
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _is_full_page(text):
            yield path.relative_to(TEMPLATES).as_posix(), text


class ViewportTests(TestCase):
    def test_every_full_page_declares_a_viewport(self):
        missing = [rel for rel, text in _pages() if 'name="viewport"' not in text]

        self.assertEqual(
            missing,
            [],
            "A full HTML page without <meta name=\"viewport\"> renders at a "
            "desktop width on phones, so everything appears tiny. Add it to: "
            f"{missing}",
        )

    def test_viewports_scale_to_the_device(self):
        wrong = [
            rel
            for rel, text in _pages()
            if 'name="viewport"' in text and "width=device-width" not in text
        ]
        self.assertEqual(wrong, [], f"Viewport must use width=device-width: {wrong}")


class WideTableTests(TestCase):
    """A table wider than its card must be reachable by scrolling."""

    CLIPPING = re.compile(r'class="[^"]*overflow-hidden[^"]*"')
    MIN_WIDTH = re.compile(r"<table[^>]*min-w-\[\d+px\]")

    def test_wide_tables_live_inside_a_scroller(self):
        offenders = []
        for path in sorted(TEMPLATES.rglob("*.html")):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for match in self.MIN_WIDTH.finditer(text):
                # Look back a short way for the enclosing container.
                window = text[max(0, match.start() - 400) : match.start()]
                if "table-scroll" in window or "overflow-x-auto" in window:
                    continue
                if "admin-table-wrap" in window:
                    continue  # that wrapper is overflow-x: auto
                offenders.append(path.relative_to(TEMPLATES).as_posix())

        self.assertEqual(
            sorted(set(offenders)),
            [],
            "A table with a fixed min-width must sit inside .table-scroll (or an "
            "overflow-x-auto container); inside a plain overflow-hidden card its "
            f"outer columns are unreachable on a phone: {sorted(set(offenders))}",
        )


class MobileStylesheetTests(TestCase):
    SHELLS = (
        "dashboard/_base.html",
        "admin/admin_base.html",
        "base.html",
        "dashboard/login.html",
    )

    def test_mobile_stylesheet_exists(self):
        self.assertTrue((STATIC_CSS / "mobile.css").exists())

    def test_every_shell_loads_it(self):
        missing = [
            shell
            for shell in self.SHELLS
            if "css/mobile.css" not in (TEMPLATES / shell).read_text(encoding="utf-8")
        ]
        self.assertEqual(
            missing,
            [],
            f"These shells miss the shared mobile corrections: {missing}",
        )

    def test_it_lifts_form_controls_to_16px(self):
        """Below 16px, iOS Safari zooms the viewport whenever a field is focused."""
        css = (STATIC_CSS / "mobile.css").read_text(encoding="utf-8")

        self.assertIn("font-size: 16px !important", css)
        self.assertIn("@media (max-width: 768px)", css)

    def test_it_provides_the_table_scroll_utility(self):
        css = (STATIC_CSS / "mobile.css").read_text(encoding="utf-8")

        self.assertRegex(css, r"\.table-scroll\s*\{[^}]*overflow-x:\s*auto")


class InvoiceMobileTests(TestCase):
    INVOICES = ("invoices/subscription_invoice.html",)
    MOBILE_SCREEN_QUERY = "@media screen and (max-width: 640px)"

    def test_invoices_are_readable_on_a_phone(self):
        for rel in self.INVOICES:
            text = (TEMPLATES / rel).read_text(encoding="utf-8")
            with self.subTest(template=rel):
                self.assertIn('name="viewport"', text)
                self.assertIn(self.MOBILE_SCREEN_QUERY, text)

    def test_invoice_summary_is_not_locked_to_a_fixed_width(self):
        """350px overflows a 360px phone once body padding is counted."""
        for rel in self.INVOICES:
            text = (TEMPLATES / rel).read_text(encoding="utf-8")
            with self.subTest(template=rel):
                _, query, following_styles = text.partition(self.MOBILE_SCREEN_QUERY)
                self.assertTrue(query)
                mobile_block = following_styles.split("@media", 1)[0]
                self.assertIn(".summary-table { width: 100%; }", mobile_block)
