"""Static checks on the demo's page, stylesheet and script (the redesign).

They read the served files; they do not run a browser or the script. Browser behaviour (layout, scrolling, theme
switching on screen) is therefore NOT proven here. What they do guard: every element the script uses exists, every
navigation link has a target, the theme is complete and readable by measured contrast, no colour is hard-coded
outside the theme blocks, nothing external is referenced, and the security-relevant rules of the page still hold.
"""

import re
import unittest
from pathlib import Path

import demo_app
from demo_app import server as server_module

STATIC = Path(demo_app.__file__).resolve().parent / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")

# The integrated workflow is the primary panel and comes first; the standalone panels follow under "supporting-tools".
SECTIONS = ("integrated", "supporting-tools", "overview", "rover", "record", "image", "history", "second-opinion",
            "research", "orchestrated", "voice-assistant")


def block_after(text, marker):
    """The text inside the braces that follow `marker` (braces are matched), and the (start, end) span."""
    start = text.index(marker)
    open_at = text.index("{", start)
    depth, i = 0, open_at
    while True:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_at + 1:i], (start, i + 1)
        i += 1


def variables(block):
    return dict(re.findall(r"--([a-z0-9-]+):\s*([^;]+);", block))


LIGHT, LIGHT_SPAN = block_after(CSS, ":root {")
MEDIA, MEDIA_SPAN = block_after(CSS, "@media (prefers-color-scheme: dark)")
SYSTEM_DARK, _ = block_after(MEDIA, ":root:not([data-theme])")
CHOSEN_DARK, DARK_SPAN = block_after(CSS, ':root[data-theme="dark"]')


def luminance(color):
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class StructureTests(unittest.TestCase):
    def test_ids_are_unique(self):
        ids = re.findall(r'\bid="([^"]+)"', HTML)
        self.assertEqual(sorted(i for i in set(ids) if ids.count(i) > 1), [])

    def test_every_element_the_script_looks_up_by_literal_id_exists(self):
        ids = set(re.findall(r'\bid="([^"]+)"', HTML))
        used = set(re.findall(r'\$\("([a-z0-9-]+)"\)', JS))
        self.assertGreater(len(used), 40)
        self.assertEqual(sorted(used - ids), [])

    def test_navigation_links_all_have_targets_and_cover_every_section(self):
        ids = set(re.findall(r'\bid="([^"]+)"', HTML))
        nav = HTML[HTML.index('<nav class="nav"'):HTML.index("</nav>")]
        targets = re.findall(r'href="#([^"]+)"', nav)
        self.assertEqual(targets, list(SECTIONS))
        for target in targets:
            self.assertIn(target, ids)
            self.assertIn('<section id="%s">' % target, HTML)
        self.assertIn('href="#main"', HTML)            # the skip link
        self.assertIn('id="main"', HTML)

    def test_the_section_titles_the_existing_tests_and_users_rely_on_are_unchanged(self):
        for heading in ("1. Mock rover", "2. Readings and Record validation", "3. Image (demo-only analysis)",
                        "4. Saved Record history", "5. Second Opinion", "6. Research (advisory)",
                        "7. Orchestrated report (fake providers)"):
            self.assertIn("<h2>%s</h2>" % heading, HTML)
        self.assertEqual(HTML.count("<h1>"), 1)

    def test_every_form_control_has_a_label_and_every_label_a_control(self):
        ids = set(re.findall(r'\bid="([^"]+)"', HTML))
        labels = re.findall(r'<label for="([^"]+)"', HTML)
        for target in labels:
            self.assertIn(target, ids)
        controls = re.findall(r'<(?:input|select)\b[^>]*\bid="([^"]+)"', HTML)
        self.assertEqual(sorted(set(controls) - set(labels)), [])

    def test_every_button_has_an_explicit_type_and_is_wired_or_submits_a_form(self):
        buttons = re.findall(r"<button\b([^>]*)>", HTML)
        self.assertEqual(len(buttons), 14)  # theme, run, analyze, refresh, second opinion, research, orchestrated, integrated, 6 voice/learning assistant buttons
        for attributes in buttons:
            self.assertRegex(attributes, r'\btype="(button|submit)"')
            ident = re.search(r'\bid="([^"]+)"', attributes).group(1)
            wired = '$("%s")' % ident in JS
            submits = 'type="submit"' in attributes
            self.assertTrue(wired or submits, ident)

    def test_the_page_has_the_basics_for_small_screens_and_assistive_technology(self):
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', HTML)
        self.assertIn('<html lang="en">', HTML)
        self.assertIn('aria-label="Sections"', HTML)
        # the seven action status lines (run, analysis, second opinion, research, orchestrated, integrated, voice assistant) are announced
        self.assertEqual(len(re.findall(r'role="status"', HTML)), 7)
        self.assertIn("@media (max-width", CSS)
        self.assertIn("overflow-x: auto", CSS)                         # wide tables and the nav scroll inside themselves
        self.assertIn(":focus-visible", CSS)
        self.assertIn("prefers-reduced-motion", CSS)
        for header in re.findall(r"<th\b([^>]*)>", HTML):
            self.assertIn('scope="col"', header)

    def test_security_relevant_rules_of_the_page_still_hold(self):
        self.assertEqual(re.findall(r"<script\b[^>]*>", HTML), ['<script src="/static/app.js">'])
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", HTML))
        self.assertNotIn("<style", HTML)
        self.assertNotIn("style=", HTML)
        for text in (HTML, CSS, JS):
            self.assertNotIn("http://", text)
            self.assertNotIn("https://", text)
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function", "document.write",
                       "localStorage", "sessionStorage", "document.cookie", "XMLHttpRequest", "WebSocket",
                       "sendBeacon", "@import", "url("):
            self.assertNotIn(banned, JS + CSS + HTML, banned)
        self.assertEqual(JS.count("fetch("), 1)

    def test_the_content_security_policy_was_not_weakened(self):
        csp = dict(server_module.SECURITY_HEADERS)["Content-Security-Policy"]
        for directive in ("default-src 'none'", "script-src 'self'", "style-src 'self'", "connect-src 'self'",
                          "base-uri 'none'", "form-action 'none'", "frame-ancestors 'none'"):
            self.assertIn(directive, csp)
        self.assertNotIn("unsafe", csp)


class ThemeTests(unittest.TestCase):
    def test_there_is_a_working_toggle_that_changes_the_theme_attribute_and_stores_nothing(self):
        self.assertIn('<button id="theme-toggle" type="button"', HTML)
        for fragment in ('$("theme-toggle")', 'addEventListener("click", toggleTheme)',
                         'setAttribute("data-theme"', '"(prefers-color-scheme: dark)"', "initTheme();"):
            self.assertIn(fragment, JS, fragment)
        self.assertIn('data-theme="dark"', CSS)
        self.assertIn(":root:not([data-theme])", CSS)

    def test_the_three_theme_blocks_define_the_same_variables_and_the_dark_ones_agree(self):
        light, system_dark, chosen_dark = variables(LIGHT), variables(SYSTEM_DARK), variables(CHOSEN_DARK)
        self.assertGreater(len(light), 20)
        self.assertEqual(set(light), set(system_dark))
        self.assertEqual(set(light), set(chosen_dark))
        self.assertEqual(system_dark, chosen_dark)                   # choosing dark equals the system dark theme
        self.assertNotEqual(light["bg"], chosen_dark["bg"])

    def test_no_colour_is_hard_coded_outside_the_theme_blocks(self):
        rest = CSS
        for start, end in sorted((LIGHT_SPAN, MEDIA_SPAN, DARK_SPAN), reverse=True):
            rest = rest[:start] + rest[end:]
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", rest))
        self.assertIsNone(re.search(r"\b(?:rgb|rgba|hsl|hsla)\(", rest))
        self.assertIn("var(--", rest)

    def test_text_colours_meet_the_normal_text_contrast_ratio_in_both_themes(self):
        pairs = (("text", "bg"), ("text", "surface"), ("text", "surface-2"), ("text", "surface-3"),
                 ("muted", "surface"), ("muted", "surface-2"), ("muted", "bg"), ("accent-ink", "accent"),
                 ("good", "good-bg"), ("bad", "bad-bg"), ("warn", "warn-bg"), ("sim", "sim-bg"),
                 ("fake", "fake-bg"), ("good", "surface"), ("bad", "surface"), ("text", "header-bg"),
                 ("good", "surface-2"), ("bad", "surface-2"), ("warn", "surface-2"))
        for name, block in (("light", LIGHT), ("dark", CHOSEN_DARK)):
            values = variables(block)
            for foreground, background in pairs:
                with self.subTest(theme=name, pair=(foreground, background)):
                    self.assertGreaterEqual(contrast(values[foreground], values[background]), 4.5)

    def test_focus_and_accent_colours_are_visible_against_their_surfaces(self):
        for block in (LIGHT, CHOSEN_DARK):
            values = variables(block)
            self.assertGreaterEqual(contrast(values["focus"], values["surface"]), 3.0)
            self.assertGreaterEqual(contrast(values["accent-2"], values["surface"]), 3.0)
            self.assertGreaterEqual(contrast(values["border"], values["surface"]), 1.2)


class SectionSevenTests(unittest.TestCase):
    def function_text(self, name):
        body = JS[JS.index("function %s(" % name):]
        return body[:body.index("\n}\n")]

    def test_section_seven_keeps_its_three_modes_its_limit_and_its_fake_warning(self):
        for mode in ("skip", "fake", "fake_error"):
            self.assertIn(mode, JS[JS.index("const OM_MODE_TEXT"):JS.index("function configureOrchestrated")])
        self.assertIn("omConfig.maxChars", JS)
        self.assertIn('<span class="tag fake">FAKE PROVIDERS</span>', HTML)
        self.assertIn('class="demo-label fake" id="om-label">FAKE PROVIDERS.', HTML)
        self.assertIn("is always skipped", HTML)
        self.assertIn('"unknown" on purpose', HTML)

    def test_fake_sources_are_labelled_fake_and_never_become_links(self):
        card = self.function_text("sourceCard")
        self.assertIn('badge("FAKE", "fake")', card)
        self.assertIn("Made-up address. It is not a real web page.", card)
        self.assertNotIn('createElement("a")', card)
        self.assertNotIn('el("a"', card)
        self.assertNotIn("href", card)
        render = self.function_text("renderOrchestrated")
        self.assertNotIn("href", render)
        self.assertIn("data.research_stage.fake === true", render)
        self.assertIn("data.image_stage.status", render)             # the skipped image stage is shown from the response

    def test_every_state_of_the_response_has_a_place_on_the_page(self):
        render = self.function_text("renderOrchestrated")
        for fragment in ("run.run_status", "run.record_id", "report.origin", "run.run_id", "run.run.steps",
                         "e.error_code", "e.skip_reason", "sources.error_code", "sources.entries",
                         "s.uncertain_or_unavailable", "s.recommendations.supported", "run.provenance",
                         "s.sensors_measured.observations", "data.data_source"):
            self.assertIn(fragment, render, fragment)
        self.assertIn("The response was not understood", JS)         # a response of the wrong shape is not displayed


class OverviewAndHistoryTests(unittest.TestCase):
    def test_overview_tiles_are_filled_only_from_state_or_history(self):
        for ident in ("ov-records", "ov-latest", "ov-faults", "ov-so", "ov-research", "ov-orch"):
            self.assertIn('id="%s"' % ident, HTML)
            self.assertIn('"%s"' % ident, JS)
        self.assertIn("configureOverview(state);", JS)
        self.assertIn("configureOverview(null);", JS)
        self.assertIn("updateOverviewFromHistory(data);", JS)

    def test_the_trend_is_scaled_to_the_listed_records_only_and_says_so(self):
        self.assertIn("not a limit or a health range", HTML)
        self.assertIn("scaled between the smallest and largest value in this list only", HTML)
        self.assertIn('document.createElement("meter")', JS)
        self.assertIn("same value in every listed Record", JS)
        self.assertIn("No Records to plot yet", HTML)               # an honest empty state, not an empty chart

    def test_readings_show_the_application_unit_text_and_say_when_a_reading_is_missing(self):
        cards = JS[JS.index("function renderReadingCards"):JS.index("function renderRun")]
        self.assertIn("r.unit", cards)
        self.assertIn("showValue(r.value)", cards)
        self.assertIn("(simulated)", cards)
        self.assertIn('"status unknown"', cards)
        self.assertIn("no reading (null)", JS)


if __name__ == "__main__":
    unittest.main()
