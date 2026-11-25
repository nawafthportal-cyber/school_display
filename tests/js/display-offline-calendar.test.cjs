// Run with: node --test tests/js/display-offline-calendar.test.cjs
// Execute the production helpers with a synthetic clock; no network or DB.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const sourcePath = path.join(__dirname, '../../static/js/display.js');
const source = fs.readFileSync(sourcePath, 'utf8');
function extract(name) {
  const start = source.indexOf('  function ' + name + '(');
  const end = source.indexOf('\n  }', start);
  assert(start >= 0 && end > start, 'Production helper not found: ' + name);
  return source.slice(start, end + 4);
}

function fixture(iso = '2026-10-13T08:30:00+03:00') {
  const now = Date.parse(iso);
  const context = {
    nowMs: () => now,
    serverTzOffsetMin: 180,
    safeText: value => String(value == null ? '' : value),
    _dayEngine: { lastDayKey: 'yesterday', blocks: ['old'] },
    cachedDateInfo: { date: '2026-10-12' },
    applyServerCalendar: () => {},
    _serverDayStartMsForBase: value => {
      const date = new Date(value + context.serverTzOffsetMin * 60000).toISOString().slice(0, 10);
      return Date.parse(date + 'T00:00:00Z') - context.serverTzOffsetMin * 60000;
    },
    dayEngineClear: () => { context._dayEngine.blocks = []; context._dayEngine.lastDayKey = ''; },
  };
  vm.createContext(context);
  for (const name of ['_offlineWallClock', '_localDateKey', '_dbWeekday', '_offlineDayStartMs',
    '_offlineMinuteOfDay', '_offlinePlanBounds', '_offlineCalendarForDate', '_offlineNextSchoolDay', '_rehydrateForNewDay']) {
    vm.runInContext(extract(name), context, { filename: sourcePath + ':' + name });
  }
  const period = { kind: 'period', index: 1, from: '08:00', to: '09:00' };
  const payload = {
    meta: {
      date: '2026-10-12',
      week_plan: { '1': [period], '2': [period], '3': [period], '4': [], '5': [], '6': [], '7': [] },
      offline_calendar: {
        version: 1, timezone_name: 'Asia/Riyadh', utc_offset_minutes: 180,
        valid_from: '2026-10-12', valid_through: '2026-11-01',
        closed_dates: { '2026-10-12': 'Known closure', '2026-10-13': 'Known closure' },
        default_holiday_title: 'Weekend',
      },
      active_window: { start: 'old', end: 'old' },
    },
    settings: { display_holiday_title: 'Old holiday title' },
    day_path: [period],
    period_classes: [{ teacher: 'Yesterday teacher' }],
    period_classes_map: { '1': [{ teacher: 'Yesterday teacher' }] },
    standby: { items: ['old'] }, duty: { items: ['old'] }, excellence: ['old'],
    current_period: period, next_period: period, date_info: { date: 'old' },
  };
  return { context, payload, rehydrate: () => context._rehydrateForNewDay(payload) };
}

test('a known dated closure wins over a populated weekly timetable tomorrow', () => {
  const f = fixture();
  const result = f.rehydrate();
  assert.equal(result.state.reason, 'holiday');
  assert.equal(result.state.label, 'Known closure');
  assert.equal(result.meta.is_school_day, false);
  assert.equal(result.day_path.length, 0);
  assert.equal(result.meta.next_school_day.date, '2026-10-14');
  assert.equal(result.meta.next_wake_at, '2026-10-14T04:30:00.000Z');
  assert.equal(f.context._dayEngine.blocks.length, 0);
});

test('empty closure titles still suppress school, and dated titles do not leak to weekends', () => {
  const f = fixture();
  f.payload.meta.offline_calendar.closed_dates['2026-10-13'] = '';
  assert.equal(f.rehydrate().state.reason, 'holiday');
  assert.equal(f.payload.state.label, 'Weekend');
  const weekend = fixture('2026-10-16T08:30:00+03:00');
  assert.equal(weekend.rehydrate().state.label, 'Weekend');
});

test('the next known school day reanchors timestamps and drops all previous daily content', () => {
  const f = fixture('2026-10-14T08:30:00+03:00');
  const result = f.rehydrate();
  assert.equal(result.state.reason, 'offline_recomputed');
  assert.equal(result.meta.date, '2026-10-14');
  assert.equal(result.meta.active_window.start, '2026-10-14T04:30:00.000Z');
  assert.equal(result.meta.is_active_window, true);
  assert.equal(result._offlineHasDayPlan, true);
  for (const key of ['period_classes', 'excellence']) assert.equal(result[key].length, 0);
  assert.equal(Object.keys(result.period_classes_map).length, 0);
  assert.equal(result.standby.items.length, 0);
  assert.equal(result.duty.items.length, 0);
  for (const key of ['current_period', 'next_period', 'date_info']) assert.equal(result[key], null);
  assert.equal(f.context.cachedDateInfo, null);
});

test('legacy snapshots keep online compatibility but cannot assert another offline school day', () => {
  const f = fixture();
  delete f.payload.meta.offline_calendar;
  const result = f.rehydrate();
  assert.equal(result.state.reason, 'offline_unknown_day');
  assert.equal(result._offlineHasDayPlan, false);
  assert.equal(result.day_path.length, 0);
  assert.equal(result.meta.next_school_day, null);
  assert.equal(result.meta.active_window, null);
  assert.equal(result.meta.next_wake_at, '2026-10-13T05:35:00.000Z');
});

test('calendar validity is inclusive and an expired calendar never revives a weekly plan', () => {
  const atBoundary = fixture();
  atBoundary.payload.meta.offline_calendar.valid_through = '2026-10-13';
  assert.equal(atBoundary.rehydrate().state.reason, 'holiday');
  assert.equal(atBoundary.payload.meta.next_school_day, null);
  const expired = fixture('2026-10-14T08:30:00+03:00');
  expired.payload.meta.offline_calendar.valid_through = '2026-10-13';
  assert.equal(expired.rehydrate().state.reason, 'offline_unknown_day');
  assert.equal(expired.payload.day_path.length, 0);
});

test('a changed timezone offset or malformed calendar cannot determine offline school times', () => {
  for (const mutate of [
    f => { f.payload.meta.offline_calendar.utc_offset_minutes = 120; },
    f => { f.payload.meta.offline_calendar.closed_dates = []; },
    f => { f.payload.meta.offline_calendar.version = 2; },
    f => { f.context.serverTzOffsetMin = null; },
  ]) {
    const f = fixture();
    mutate(f);
    assert.equal(f.rehydrate().state.reason, 'offline_unknown_day');
    assert.equal(f.payload.meta.next_school_day, null);
  }
});

test('missing weekday data is unknown and cannot promise a later next school day', () => {
  const f = fixture();
  delete f.payload.meta.week_plan['3'];
  assert.equal(f.rehydrate().meta.next_school_day, null);
  const today = fixture('2026-10-14T08:30:00+03:00');
  delete today.payload.meta.week_plan['3'];
  assert.equal(today.rehydrate().state.reason, 'offline_unknown_day');
});

test('an unusable timetable is unknown rather than a confirmed holiday', () => {
  const f = fixture('2026-10-14T08:30:00+03:00');
  f.payload.meta.week_plan['3'] = [{ kind: 'period', from: 'invalid', to: 'invalid' }];
  const result = f.rehydrate();
  assert.equal(result.state.reason, 'offline_unknown_day');
  assert.equal(result.day_path.length, 0);
});

test('rendered messages distinguish off-hours, a dated closure and configured holiday copy', () => {
  function renderedTitle(iso, shifted, update = () => {}) {
    const f = fixture(iso);
    const payload = f.rehydrate();
    if (!shifted) delete payload._offlineDayShifted;
    update(payload);
    const noOp = () => {};
    Object.assign(f.context, {
      dom: { heroTitle: {}, heroRange: {}, badgeKind: {} },
      rt: {}, cfg: {}, last: {},
      DISPLAY_COPY: {
        holidayTitle: 'Old holiday title', holidayBadge: 'Holiday',
        afterTitle: 'See you tomorrow', afterBadge: 'Complete',
        afterHolidayTitle: 'Enjoy the holiday', afterHolidayBadge: 'Holiday',
      },
      periodsScroller: null, standbyScroller: null, dutyScroller: null,
      hasActiveCountdown: false, countdownSeconds: null, lastStateCoreSig: '',
      lastZeroHandledCoreSig: '', progressRange: {},
      getStateReason: state => state.reason,
      getPeriodIndex: () => null, getCurrentPeriodIdxFromPayload: () => null,
      computeDayOver: () => false, motionSpeed: () => 0.5,
      fmtTimeRange: () => '--:--',
      setTextIfChanged: (element, value) => { if (element) element.textContent = value; },
      renderEmergencyAlerts: noOp, renderOfflineMode: noOp, hydrateBrand: noOp,
      dayEngineLoad: noOp, applyDisplayCopySettings: noOp, tickClock: noOp,
      checkStateTransitionBell: noOp, ingestPeriodClassesFromPayload: noOp,
      applyBoardStatePresentation: noOp, renderCurrentChips: noOp,
      renderMiniSchedule: noOp, dayEngineSyncToLocalNow: noOp, sleepEvaluate: noOp,
    });
    for (const name of ['normalizeDisplayStateType', 'getAfterDayOverCopy', 'renderState']) {
      vm.runInContext(extract(name), f.context, { filename: sourcePath + ':' + name });
    }
    f.context.renderState(payload);
    return f.context.dom.heroTitle.textContent;
  }
  assert.equal(renderedTitle('2026-10-13T08:30:00+03:00', true), 'Known closure');
  assert.equal(renderedTitle('2026-10-16T08:30:00+03:00', true), 'Weekend');
  assert.equal(renderedTitle('2026-10-13T08:30:00+03:00', false), 'Old holiday title');
  assert.equal(renderedTitle('2026-10-13T08:30:00+03:00', false, payload => {
    payload.state = { type: 'NO_SCHEDULE_TODAY', reason: 'holiday', label: 'Known closure', variant: 'dated_closure' };
  }), 'Known closure');
  assert.equal(renderedTitle('2026-10-13T20:30:00+03:00', false, payload => {
    payload.state = { type: 'OFF_HOURS', reason: 'after_hours' };
    payload.meta.next_school_day = { days_ahead: 1 };
  }), 'See you tomorrow');
});

test('unknown calendar recovery uses five-minute base polling while terminal and sleep gates remain authoritative', () => {
  const f = fixture();
  Object.assign(f.context, {
    isTerminalBlockedMode: () => false,
    rt: { mode: 'fallback-poll', isActiveWindow: false, ws: null },
    cfg: { REFRESH_EVERY: 20, WS_LIVE_STATUS_CHECK_SEC: 3600 },
    WebSocket: { OPEN: 1 },
    lastPayloadForFiltering: { state: { reason: 'offline_unknown_day' } },
    getStateReason: state => state.reason,
    getFallbackPollDelay: () => 180,
  });
  vm.runInContext(extract('basePollEverySec'), f.context, { filename: sourcePath + ':basePollEverySec' });
  assert.equal(f.context.basePollEverySec(), 300);
  f.context.rt.ws = { readyState: 1 };
  assert.equal(f.context.basePollEverySec(), 300);
  f.context.rt.mode = 'sleeping';
  assert.equal(f.context.basePollEverySec(), 86400);
  f.context.rt.mode = 'fallback-poll';
  f.context.isTerminalBlockedMode = () => true;
  assert.equal(f.context.basePollEverySec(), 86400);
  f.context.isTerminalBlockedMode = () => false;
  f.context.lastPayloadForFiltering.state.reason = 'after_hours';
  assert.equal(f.context.basePollEverySec(), 3600);
  f.context.rt.ws = null;
  assert.equal(f.context.basePollEverySec(), 1800);
  f.context.rt.isActiveWindow = true;
  assert.equal(f.context.basePollEverySec(), 180);
});
