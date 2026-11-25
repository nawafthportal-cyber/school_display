"""The display page must serve the bundle the settings ask for."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import DisplayScreen, School, SubscriptionPlan
from schedule.models import SchoolSettings
from subscriptions.models import SchoolSubscription


class DisplayBundleSelectionTests(TestCase):
    def setUp(self):
        self.school = School.objects.create(name="مدرسة الحزمة", slug="bundle-school")
        self.settings_obj = SchoolSettings.objects.create(school=self.school, name=self.school.name)
        self.screen = DisplayScreen.objects.create(school=self.school, name="شاشة", is_active=True)
        plan = SubscriptionPlan.objects.create(
            code="bundle-plan",
            name="خطة",
            price=Decimal("100.00"),
            duration_days=365,
            max_screens=3,
        )
        SchoolSubscription.objects.create(
            school=self.school,
            plan=plan,
            starts_at=timezone.localdate(),
            status="active",
        )

    def _page(self):
        return self.client.get(reverse("website:short_display", args=[self.screen.short_code]))

    @override_settings(DISPLAY_USE_MINIFIED_JS=True)
    def test_minified_bundle_is_served_by_default(self):
        response = self._page()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "js/display.min.js")

    @override_settings(DISPLAY_USE_MINIFIED_JS=False)
    def test_readable_bundle_can_be_restored_for_debugging(self):
        response = self._page()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "js/display.js")
        self.assertNotContains(response, "js/display.min.js")

    @override_settings(DISPLAY_USE_MINIFIED_JS=True)
    def test_service_worker_registration_is_external(self):
        """Inline registration would be blocked by the enforced CSP."""
        response = self._page()

        self.assertContains(response, "js/display-sw-register.js")

    def test_selected_bell_sound_is_served_to_display_page(self):
        self.settings_obj.bell_sound = "elevator_announcement_bells"
        self.settings_obj.save(update_fields=["bell_sound"])

        response = self._page()

        self.assertContains(response, "mixkit-elevator-announcement-bells-112.wav")


class DisplayPageWeightTests(DisplayBundleSelectionTests):
    """The board's first paint must not wait on anything it does not control."""

    def test_no_render_blocking_third_party_stylesheet(self):
        """A filtered or slow school network must not delay the first frame.

        The page used to pull IBM Plex Sans Arabic from fonts.googleapis.com as a
        render-blocking cross-origin stylesheet, which the Service Worker cannot
        cache either. The faces are served from our own origin now.
        """
        html = self._page().content.decode("utf-8")

        self.assertNotIn("fonts.googleapis.com/css", html)
        self.assertNotIn("fonts.gstatic.com", html)
        self.assertIn("css/fonts.css", html)

    def test_board_styles_are_a_cacheable_file(self):
        """~100 KB of inline <style> was re-sent and re-parsed on every load.

        Only the per-school occasion palettes generated from core/occasions.py
        may stay inline, and they have to come after the stylesheet so they keep
        overriding it.
        """
        html = self._page().content.decode("utf-8")

        self.assertIn("css/display-board.css", html)

        inline_css = "".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))
        self.assertLess(
            len(inline_css),
            12_000,
            "inline CSS is creeping back into the display page; it belongs in display-board.css",
        )
        self.assertIn("--occasion-accent", inline_css)
        self.assertLess(
            html.index("css/display-board.css"),
            html.index("--occasion-accent"),
            "occasion palettes must be emitted after display-board.css to win the cascade",
        )

    def test_lite_mode_is_resolved_server_side_for_televisions(self):
        """A TV must never paint the heavy design, not even for one frame.

        display.js can detect the device itself, but only after a deferred
        bundle parses — long after the browser has painted the blurred orbs and
        glass panels once.
        """
        tv = self.client.get(
            reverse("website:short_display", args=[self.screen.short_code]),
            HTTP_USER_AGENT="Mozilla/5.0 (SMART-TV; Linux; Tizen 5.5) AppleWebKit/537.36",
        )
        self.assertIn('data-lite="1"', tv.content.decode("utf-8"))

    def test_lite_mode_is_left_to_the_client_on_unknown_devices(self):
        """An empty value hands the decision to the client's hardware heuristics."""
        desktop = self.client.get(
            reverse("website:short_display", args=[self.screen.short_code]),
            HTTP_USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36",
        )
        self.assertIn('data-lite=""', desktop.content.decode("utf-8"))

    def test_lite_mode_query_override_wins_in_both_directions(self):
        url = reverse("website:short_display", args=[self.screen.short_code])
        tv_ua = "Mozilla/5.0 (SMART-TV; Linux; Tizen 5.5) AppleWebKit/537.36"
        pc_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"

        forced_on = self.client.get(f"{url}?lite=1", HTTP_USER_AGENT=pc_ua)
        self.assertIn('data-lite="1"', forced_on.content.decode("utf-8"))

        forced_off = self.client.get(f"{url}?lite=0", HTTP_USER_AGENT=tv_ua)
        self.assertIn('data-lite="0"', forced_off.content.decode("utf-8"))

    def test_lite_mode_never_enters_the_shared_context_cache(self):
        """The context dict is cached per token+revision; lite mode is per device."""
        url = reverse("website:short_display", args=[self.screen.short_code])
        tv_ua = "Mozilla/5.0 (SMART-TV; Linux; Tizen 5.5) AppleWebKit/537.36"
        pc_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0 Safari/537.36"

        # Warm the cache from the TV, then make sure a desktop is not served its answer.
        self.assertIn('data-lite="1"', self.client.get(url, HTTP_USER_AGENT=tv_ua).content.decode("utf-8"))
        self.assertIn('data-lite=""', self.client.get(url, HTTP_USER_AGENT=pc_ua).content.decode("utf-8"))
        self.assertIn('data-lite="1"', self.client.get(url, HTTP_USER_AGENT=tv_ua).content.decode("utf-8"))


class DisplayBoardStylesheetTests(SimpleTestCase):
    """Guards on the extracted stylesheet itself."""

    def setUp(self):
        self.css = (
            Path(settings.BASE_DIR) / "static" / "css" / "display-board.css"
        ).read_text(encoding="utf-8")

    def test_lite_mode_covers_the_expensive_board_effects(self):
        """These four were only ever disabled by `prefers-reduced-motion`.

        No television reports that preference, so the weakest devices in the
        fleet kept paying for the most expensive effects on the page.
        """
        for selector in (
            'body[data-lite="1"] .hero-card__orb',
            'body[data-lite="1"] .hero-card__ambient',
            'body[data-lite="1"] .header-live-status__pulse',
            'body[data-lite="1"] .countdown-idle-glyph',
        ):
            with self.subTest(selector=selector):
                self.assertIn(selector, self.css)

    def test_stage_avoids_permanent_layer_promotion_and_forced_kerning(self):
        """#fitStage is a 1920×1080 canvas holding every backdrop-filter panel.

        `will-change: transform` pinned that as a GPU texture for the whole
        session even though the fit scale only changes on a viewport resize, and
        `optimizeLegibility` forced kerning for every glyph on every layout pass.
        """
        stage = self.css.split("#fitStage {", 1)[1].split("}", 1)[0]
        declarations = re.sub(r"/\*.*?\*/", "", stage, flags=re.S)

        self.assertNotIn("will-change", declarations)
        self.assertNotIn("optimizeLegibility", declarations)
        # The scrollers still animate every frame, so they keep their promotion.
        self.assertIn(".track {", self.css)

    def test_no_django_template_syntax_leaked_into_the_static_file(self):
        """The occasion loops must have stayed behind in the template."""
        self.assertNotIn("{%", self.css)
        self.assertNotIn("{{", self.css)

    def test_board_tokens_are_scoped_where_the_theme_variables_live(self):
        """Derived board tokens must not be computed on :root.

        app.css declares --accent-main/--accent-sub/--mesh1/--mesh2 on
        `body.display-board`. A `:root` block reading them resolves to the indigo
        fallback instead — and then inherits that down to the whole board, so the
        2026 layer stayed indigo whatever theme the school picked.
        """
        stripped = re.sub(r"/\*.*?\*/", "", self.css, flags=re.S)
        theme_vars = r"--(?:accent-main|accent-sub|mesh1|mesh2|glass-surface|glass-border|bg-deep)"

        offenders = []
        for match in re.finditer(r"(^|\n)([ \t]*):root\s*\{(.*?)\n\2\}", stripped, re.S):
            if re.search(rf"var\(\s*{theme_vars}", match.group(3)):
                offenders.append(stripped[: match.start()].count("\n") + 2)

        self.assertEqual(
            offenders,
            [],
            f":root blocks at lines {offenders} read variables declared on body.display-board",
        )

    def test_board_tokens_are_declared_and_balanced(self):
        self.assertIn("--board-tint-1", self.css)
        self.assertIn("--board-accent-border", self.css)
        stripped = re.sub(r"/\*.*?\*/", "", self.css, flags=re.S)
        self.assertEqual(stripped.count("{"), stripped.count("}"))

    def test_every_palette_class_in_the_markup_is_themed_or_semantic(self):
        """A theme choice must reach the whole board.

        Fixed Tailwind palette classes are allowed only where the colour carries
        meaning — the emergency banner has to read as an alarm no matter what
        palette the school picked.
        """
        markup = (
            Path(settings.BASE_DIR) / "templates" / "website" / "display.html"
        ).read_text(encoding="utf-8").split("<body", 1)[1]
        tailwind = (
            Path(settings.BASE_DIR) / "static" / "css" / "tailwind.generated.css"
        ).read_text(encoding="utf-8")

        palette = r"(?:emerald|orange|amber|purple|red|rose|cyan|sky|violet|teal|lime|yellow|green|blue)"
        exempt = {
            # Emergency banner: red has to read as an alarm, whatever the palette.
            "bg-red-500", "bg-red-500/10", "text-red-200",
            # Already neutralised by `aside .glass-panel > div:first-child`, which
            # gives both side-panel headers the same white wash at a higher
            # specificity. An override keyed on the utility could never win.
            "bg-orange-500/5",
        }

        def defined_in(sheet, utility):
            """Exact class match — `bg-orange-500/5` must not match `/50`."""
            escaped = re.escape(utility.replace("/", r"\/"))
            return re.search(rf"\.{escaped}(?![\w\\/-])", sheet) is not None

        unthemed = []
        for utility in sorted(set(re.findall(
            rf"\b(?:bg|text|border|from|to|via)-{palette}-\d{{2,3}}(?:/\d{{1,3}})?", markup
        ))):
            if utility in exempt:
                continue
            # A class Tailwind never generated paints nothing, so it cannot
            # override the theme either.
            if not defined_in(tailwind, utility):
                continue
            if not defined_in(self.css, utility):
                unthemed.append(utility)

        self.assertEqual(
            unthemed,
            [],
            f"these classes render a fixed colour the theme cannot reach: {unthemed}",
        )


def _function_source(js: str, name: str) -> str:
    """Return the source of a top-level `function name(...) { ... }` in `js`."""
    start = js.index(f"function {name}(")
    depth, i = 0, js.index("{", start)
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[start : j + 1]
    raise AssertionError(f"could not delimit {name}")


class DisplaySleepEngineTests(SimpleTestCase):
    """The sleep engine decides when a television goes dark and when it comes back.

    Every assertion here stands for a screen that stayed dark when it should not
    have, or a fleet that hammered the server when it should have been quiet.
    The project has no JavaScript test runner, so these pin the wiring at source
    level in the same style as the other display.js guards in this suite.
    """

    def setUp(self):
        self.js = (
            Path(settings.BASE_DIR) / "static" / "js" / "display.js"
        ).read_text(encoding="utf-8")

    def _body(self, name):
        return _function_source(self.js, name)

    def test_sleep_is_entered_only_for_a_reason_the_server_named(self):
        """A school day whose timetable is empty is a fault, not a night.

        `state.type == "off"` also covers `no_timeline`, so any misconfigured day
        used to put the screen to sleep until tomorrow's wake — an admin fixing
        the schedule at 10:00 could not get the board back before the next day.
        """
        body = self._body("sleepEvaluate")

        self.assertIn('stateReason === "before_hours" || stateReason === "after_hours"', body)
        self.assertNotIn('stateType === "off" || stateReason === "before_hours"', body)

    def test_a_windowless_off_state_never_sleeps(self):
        """The legacy `off` path needs both edges of the window to place itself."""
        body = self._body("sleepEvaluate")

        self.assertIn(
            'stateType === "off" && rt.activeWindowStartMs && rt.activeWindowEndMs',
            body,
        )

    def test_a_window_start_already_behind_us_is_not_a_wake_target(self):
        """Otherwise sleep→wake→fetch→sleep spins for as long as it is sent."""
        body = self._body("sleepEvaluate")

        self.assertIn("rt.activeWindowStartMs > now", body)

    def test_both_sleep_entry_points_arm_the_wake_through_one_path(self):
        """A throttle honoured on one path and bypassed on the other is no throttle."""
        for name in ("sleepEnter", "sleepSafetyCheck"):
            with self.subTest(function=name):
                self.assertIn("_armSleepWake(", self._body(name))

    def test_a_wake_target_in_the_past_wakes_at_most_once_per_floor(self):
        body = self._body("_armSleepWake")

        self.assertIn("recheckMs = wakeMs ? STALE_WAKE_MIN_INTERVAL_MS : SLEEP_CHECK_MS", body)
        self.assertIn("rt.lastSleepWakeAt = now", body)

    def test_every_wake_taken_while_asleep_anchors_the_throttle(self):
        """A scheduled re-check and the throttle would each fire at the boundary."""
        self.assertIn("rt.lastSleepWakeAt = nowMs();", self._body("_scheduleCappedWake"))
        self.assertIn("rt.lastSleepWakeAt = nowMs();", self._body("sleepEnter"))

    def test_a_payload_naming_no_wake_moment_still_gets_re_checked(self):
        """Sleeping forever on it would need a power cycle to recover."""
        body = self._body("_armSleepWake")

        self.assertIn(": SLEEP_CHECK_MS", body)
        self.assertIn('setMode("waking"', body)

    def test_a_socket_lost_during_sleep_is_re_established(self):
        """The pre-active wake is a server push; polling is off until it lands.

        `_scheduleSleepReconnect` existed but was never called, and
        `initWebSocket` refused to connect while sleeping — so one overnight
        disconnect left the screen unreachable until its own timer fired.
        """
        self.assertGreaterEqual(self.js.count("_scheduleSleepReconnect();"), 4)
        self.assertIn("_scheduleSleepReconnect();", self._body("_closeWsForSleep"))
        self.assertIn("initWebSocket({ duringSleep: true })", self._body("_scheduleSleepReconnect"))
        self.assertIn('rt.mode === "sleeping" && !duringSleep', self._body("initWebSocket"))

    def test_sleep_reconnects_back_off_to_a_ceiling(self):
        """A server down at 02:00 must not meet the whole fleet on a 5s retry."""
        body = self._body("_sleepReconnectDelayMs")

        self.assertIn("SLEEP_RECONNECT_CAP_MS", body)
        self.assertIn("Math.pow(2,", body)

    def test_a_night_of_failed_reconnects_does_not_delay_the_morning_connect(self):
        """Ten overnight failures would put the first connect of the day behind
        the 120s slow-retry timer, on the one morning the board must be live."""
        body = self._body("_onModeEnter")

        self.assertIn("rt.wsRetryCount = 0;", body)
        self.assertIn("rt.sleepReconnectAttempts = 0;", body)

    def test_the_keepalive_ping_is_restored_when_a_socket_survives_sleep(self):
        """Sleep pauses the client ping; waking with the same socket must resume it."""
        self.assertIn('_startWsKeepalive("wake")', self._body("_onModeEnter"))
        self.assertIn('_log("ws_keepalive_skipped", { reason: "sleeping" });', self.js)


class DisplayPushFirstCadenceTests(SimpleTestCase):
    """The board is driven by pushes, not by polling.

    Content changes when a manager saves something, and that arrives over the
    WebSocket; period and break transitions are computed from the day plan the
    screen already holds. The HTTP checks that remain are proof of life, and at
    one per minute they were the single largest source of display traffic in the
    system — about 420 requests per screen per school day, each one answered
    "nothing changed".
    """

    def setUp(self):
        self.js = (
            Path(settings.BASE_DIR) / "static" / "js" / "display.js"
        ).read_text(encoding="utf-8")

    def _body(self, name):
        return _function_source(self.js, name)

    def test_the_safety_check_can_be_configured_up_to_an_hour(self):
        """The client used to clamp this to five minutes whatever the server said."""
        self.assertIn("WS_LIVE_STATUS_CHECK_SEC: 3600", self.js)
        self.assertIn("Math.max(5, Math.min(3600, sec))", self._body("_wsLiveStatusIntervalMs"))
        self.assertIn('parseFloat(body.dataset.wsLiveStatusCheck || "3600") || 3600', self.js)

    def test_the_server_default_matches_the_client_ceiling(self):
        """A mismatch here silently reinstates the old traffic on every screen."""
        self.assertEqual(settings.DISPLAY_WS_LIVE_STATUS_CHECK_SEC, 3600)

    def test_a_live_socket_replaces_the_periodic_poll(self):
        body = self._body("basePollEverySec")

        self.assertIn("Math.max(300, Number(cfg.WS_LIVE_STATUS_CHECK_SEC) || 3600)", body)
        for stale in ("if (_dayEngine.blocks.length > 0) return 300;", "return 1800;\n        // ws-live"):
            with self.subTest(stale=stale):
                self.assertNotIn(stale, body)

    def test_the_first_check_after_reconnecting_stays_immediate(self):
        """A push sent while the socket was down is gone; this is what recovers it."""
        self.assertIn("WS_LIVE_STATUS_FIRST_CHECK_SEC = 5", self.js)
        self.assertIn(
            '_startWsLiveStatusSafeguard("ws_live_entered", { firstCheck: true })',
            self._body("_onModeEnter"),
        )
        self.assertIn("if (firstCheck) {", self._body("_wsLiveStatusIntervalMs"))

    def test_the_hourly_check_is_spread_across_the_fleet(self):
        """Fixed seconds of jitter on an hourly timer still lands everyone together."""
        body = self._body("_wsLiveStatusIntervalMs")

        self.assertIn("Math.min(600, sec * 0.4)", body)

    def test_a_clock_jump_still_forces_a_resync_over_a_live_socket(self):
        """WebSocket frames carry no server time, so the socket cannot fix a jump.

        Skipping the re-sync while connected was harmless when the display
        polled every minute. At one poll an hour it would leave the bell and
        every countdown running on the television's own wrong clock.
        """
        body = self._body("requestReSyncIfNeeded")

        self.assertIn("rt.wsConnected && !clockDriftDetected", body)
        self.assertIn("if (!clockDriftDetected && lastServerSyncAt > 0", body)


class DisplayEndOfDayPanelTests(SimpleTestCase):
    """Day-scoped panels must switch off once the school day is over.

    Verified end to end in headless Chrome against a stubbed `after` snapshot:
    with the previous bundle the honour board stayed VISIBLE and featuredEmpty
    was "0"; it now reports HIDDEN and "1", alongside the standby card, the
    period-classes card and the whole side column.

    These assertions pin the source-level invariants behind that result, in the
    same style as the other display.js guards in this suite — the project has no
    JavaScript test runner, so the browser check is a manual step and these keep
    the wiring from silently regressing.
    """

    def setUp(self):
        self.js = (
            Path(settings.BASE_DIR) / "static" / "js" / "display.js"
        ).read_text(encoding="utf-8")

    def _body(self, name):
        return _function_source(self.js, name)

    def test_featured_panel_is_gated_on_day_over(self):
        """The honour and duty boards were the only panels with no day-over gate."""
        body = self._body("renderFeaturedPanel")

        self.assertIn("rt.dayOver", body)
        for flag in ("screenShowDuty", "screenShowExcellence"):
            with self.subTest(flag=flag):
                self.assertRegex(
                    body,
                    rf"!dayOver && screenFlag\(\"{flag}\"",
                    f"{flag} must be combined with the day-over gate",
                )

    def test_featured_panel_renders_the_gated_lists(self):
        """A hidden honour board must not keep its rotation interval running."""
        body = self._body("renderFeaturedPanel")

        self.assertIn("renderDuty({ items: dutyRaw })", body)
        self.assertIn("renderExcellence(excellenceRaw)", body)

    def test_standby_and_period_lists_clear_when_the_day_is_over(self):
        standby = self._body("renderStandby")
        self.assertIn("const visible = !rt.dayOver &&", standby)
        self.assertIn("standbyGroups(visible && Array.isArray(items) ? items : [], nowMs())", standby)
        self.assertIn("if (rt.dayOver) arr = [];", self._body("renderPeriodClasses"))

    def test_day_over_transition_clears_every_day_scoped_panel(self):
        """The local clock, not the next poll, must clear the board.

        dayEngineApplyDayOver fires the moment the last block ends. Without
        these calls the board kept showing standby rows and the honour board
        until the next payload — up to a full out-of-hours poll interval.
        """
        body = self._body("dayEngineApplyDayOver")

        for call in ("renderPeriodClasses([])", "renderStandby([])", "renderFeaturedPanel("):
            with self.subTest(call=call):
                self.assertIn(call, body)

    def test_first_period_transition_reveals_preloaded_featured_panel(self):
        body = self._body("dayEngineApplyBlock")

        self.assertIn("dayEngineIsWithinPeriodDay(nowMsVal)", body)
        self.assertIn("renderFeaturedPanel(lastPayloadForFiltering)", body)

    def test_local_panel_window_uses_first_and_last_period_boundaries(self):
        body = self._body("dayEngineIsWithinPeriodDay")

        self.assertIn('!== "period"', body)
        self.assertIn("currentMs >= firstPeriodStart && currentMs < lastPeriodEnd", body)

    def test_day_over_excludes_states_where_the_day_has_not_finished(self):
        """`holiday` and `before_hours` are not "the day ended"."""
        body = self._body("computeDayOver")

        self.assertIn('if (stType === "holiday" || stateReason === "holiday" || stateReason === "before_hours") return false;', body)
        self.assertIn('if (stType === "off" || stType === "after" || stateReason === "after_hours") return true;', body)


class LegacyBrowserSupportTests(SimpleTestCase):
    """The board must survive the TV browsers display.js is built for.

    display.js is deliberately compiled for old panels — Samsung Tizen 3.0/4.0
    (Chrome 56) and LG webOS 4 (Chrome 53) — and carries polyfills to match. The
    stylesheet had no such floor: clamp() (Chrome 79), color-mix() (Chrome 111),
    grid (Chrome 57) and flex gap (Chrome 84) all shipped without a fallback, so
    the panels the JS supported still rendered a broken board.

    Verified in headless Chrome: the legacy sheet leaves a modern browser's
    computed styles byte-identical, and with its @supports gates stripped the
    board switches to flex, keeps the same pixel font sizes and picks up
    margin-based spacing.
    """

    def setUp(self):
        base = Path(settings.BASE_DIR)
        self.js = (base / "static" / "js" / "display.js").read_text(encoding="utf-8")
        self.bundle = (base / "static" / "js" / "display.min.js").read_text(
            encoding="utf-8", errors="replace"
        )
        self.legacy = (base / "static" / "css" / "display-legacy.css").read_text(encoding="utf-8")

    def test_bundle_is_built_for_es2015(self):
        """async/await is a syntax error below Chrome 55 and kills the whole bundle."""
        package = json.loads((Path(settings.BASE_DIR) / "package.json").read_text(encoding="utf-8"))

        self.assertIn("--target=es2015", package["scripts"]["build:display"])
        self.assertNotRegex(
            self.bundle,
            r"\basync\s+function|\basync\s*\(|\bawait\s",
            "the shipped bundle still contains async/await; rebuild with npm run build:display",
        )

    def test_post_chrome_49_apis_are_polyfilled_or_guarded(self):
        for api in ("Promise.prototype.finally", "String.prototype.padStart", "Object.entries"):
            with self.subTest(api=api):
                self.assertIn(f'typeof {api} !== "function"', self.js)

        # AbortController (Chrome 66) has no polyfill; it must stay optional.
        self.assertIn("window.AbortController ? new AbortController() : null", self.js)

    def test_flex_gap_is_feature_detected_rather_than_feature_queried(self):
        """`@supports (gap: 1px)` is true from Chrome 57 but flex ignored it until 84."""
        self.assertIn("function supportsFlexGap()", self.js)
        self.assertIn('body.dataset.nogap = "1"', self.js)
        self.assertIn('body[data-nogap="1"]', self.legacy)

    def test_legacy_rules_are_gated_so_modern_browsers_apply_nothing(self):
        """Everything except the JS-driven gap fallback must sit behind @supports."""
        stripped = re.sub(r"/\*.*?\*/", "", self.legacy, flags=re.S)

        # Remove every @supports block, then whatever is left must be keyed on
        # data-nogap — the one case @supports cannot express.
        def drop_supports(text):
            out, i = [], 0
            while True:
                match = re.search(r"@supports[^{]*\{", text[i:])
                if not match:
                    out.append(text[i:])
                    return "".join(out)
                start, body_start = i + match.start(), i + match.end()
                out.append(text[i:start])
                depth, j = 1, body_start
                while j < len(text) and depth:
                    if text[j] == "{":
                        depth += 1
                    elif text[j] == "}":
                        depth -= 1
                    j += 1
                i = j

        remaining = drop_supports(stripped)
        for selector, _ in re.findall(r"([^{}]+)\{([^{}]*)\}", remaining):
            with self.subTest(selector=selector.strip()[:60]):
                self.assertIn(
                    'data-nogap="1"',
                    selector,
                    "ungated legacy rule would change modern rendering",
                )

    def test_legacy_sheet_is_regenerated_from_the_current_board_css(self):
        """Fails when display-board.css changes without rebuilding the fallbacks."""
        from core.management.commands.build_legacy_css import Command

        source = (
            Path(settings.BASE_DIR) / "static" / "css" / "display-board.css"
        ).read_text(encoding="utf-8")
        generated, _ = Command().build(source)

        self.assertEqual(
            generated,
            self.legacy,
            "display-legacy.css is stale. Run: python manage.py build_legacy_css",
        )

    def test_clamp_fallbacks_reproduce_the_design_width_values(self):
        """A spot check that the evaluator is right, not just deterministic.

        #heroTitle is clamp(2.5rem, 5vw, 4rem): at the 1920px design canvas the
        preferred 96px is above the 64px ceiling, so the fallback must be 64px.
        """
        from core.management.commands.build_legacy_css import resolve_clamp

        self.assertEqual(resolve_clamp("clamp(2.5rem, 5vw, 4rem)"), "64px")
        self.assertEqual(resolve_clamp("clamp(2.15rem, 3vw, 3.35rem) !important"), "53.6px !important")
        # Percentages depend on the parent box and must be refused, not guessed.
        self.assertIsNone(resolve_clamp("clamp(10%, 5vw, 40%)"))

    def test_color_mix_fallback_keeps_nested_var_intact(self):
        """A non-greedy regex stops at the inner `var(` and mangles the value."""
        from core.management.commands.build_legacy_css import resolve_color_mix

        self.assertEqual(
            resolve_color_mix("color-mix(in srgb, var(--accent-main, #6366f1) 54%, #ffffff 16%)"),
            "var(--accent-main, #6366f1)",
        )
        self.assertEqual(
            resolve_color_mix("border-color: color-mix(in srgb, var(--x) 24%, transparent)"),
            "border-color: var(--x)",
        )

    def test_clamp_in_custom_property_gets_a_usable_legacy_value(self):
        from core.management.commands.build_legacy_css import Command

        generated, stats = Command().build(
            ".photo { --photo-size: clamp(20px, 2vw, 60px); width: var(--photo-size); }"
        )
        self.assertIn("--photo-size: 38.4px;", generated)
        self.assertEqual(stats["clamp"], 1)

    def test_grid_layout_has_a_flex_fallback(self):
        """Without it the two board columns stack full width on Chrome 56."""
        self.assertIn("@supports not (display: grid)", self.legacy)
        for selector in ("main.grid", "#boardMainColumn", "#boardSideColumn", ".header-shell"):
            with self.subTest(selector=selector):
                self.assertIn(selector, self.legacy)
