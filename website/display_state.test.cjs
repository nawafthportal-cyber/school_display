// Execute production state functions with a controlled school clock and UI adapters.
// Run directly: node --test website/display_state.test.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const filename = path.join(__dirname, '..', 'static', 'js', 'display.js');
const source = fs.readFileSync(filename, 'utf8');
function productionFunction(name) {
  const start = source.indexOf('  function ' + name + '(');
  assert.ok(start >= 0, 'Missing production function: ' + name);
  const end = source.indexOf('\n  }', start);
  assert.ok(end > start, 'Missing function end: ' + name);
  return source.slice(start, end + 4);
}

function harness() {
  let clock = Date.parse('2026-10-12T08:30:00+03:00');
  const timers = new Map();
  const ui = { bells: 0, rows: [] };
  const ctx = {
    console, Date, Set, nowMs: () => clock,
    _nc: (value, fallback) => value == null ? fallback : value,
    safeText: value => String(value == null ? '' : value),
    _dayEngine: { blocks: [], lastDayKey: '', ringPoints: [] },
    serverTzOffsetMin: 180, serverDayStartMs: null,
    bellBoundarySeen: {}, lastBellBoundaryDaySig: '', lastBellBoundaryCheckMs: 0,
    lastBoundaryBellStateKey: '', lastBoundaryBellAt: 0, lastBellStateKey: '',
    lastPayloadForFiltering: null, lastStateCoreSig: '', lastZeroHandledCoreSig: '',
    last: { stateSig: '', nextSig: '' },
    periodClassesByIndex: Object.create(null), periodClassesDayKey: '', periodClassesScheduleKey: '',
    rt: { activeStateType: 'period', activePeriodIndex: 1, activeFromHM: '08:00', activeToHM: '09:00' },
    countdownSeconds: 1800, hasActiveCountdown: true, progressRange: { start: 1, end: 2 },
    dom: { heroTitle: {}, heroRange: {}, badgeKind: {} },
    DISPLAY_COPY: { beforeBadge: 'Before', beforeTitle: 'Before school' },
    getAfterDayOverCopy: () => ({ title: 'Day complete', badge: 'Complete' }),
    formatPeriodTitle: block => 'Period ' + block.index,
    fmtTimeRange: (from, to) => from + '-' + to,
    setTextIfChanged: (element, value) => { element.textContent = value; },
    clearNamedTimer: name => timers.delete(name),
    setNamedTimer: (name, callback, delay) => timers.set(name, { callback, delay }),
    playBellSound: () => { ui.bells++; },
    renderPeriodClasses: rows => { ui.rows = Array.from(rows); },
    applyBoardStatePresentation() {}, renderStandby() {}, renderFeaturedPanel() {},
    renderMiniSchedule() {}, renderCurrentChips() {}, renderNextLabel() {}, _log() {},
  };
  vm.createContext(ctx);
  [
    'normalizeDisplayStateType', 'getStateReason', 'getPeriodIndex',
    '_offlineWallClock', '_localDateKey', '_serverDayStartMsForBase', 'hmToMs',
    '_snapshotScheduleDate', '_snapshotBlocksLocalSchedule',
    'dayEngineLoad', 'dayEngineFindNow', 'dayEngineClear', 'dayEngineSyncToLocalNow',
    'dayEngineDesiredSlotSig', 'dayEngineRuntimeSlotSig', 'dayEngineResolvePeriodIndex',
    'dayEngineIsWithinPeriodDay', 'dayEngineApplyBlock', 'dayEngineApplyDayOver', 'dayEngineOnZero',
    'checkStateTransitionBell', 'tickBoundaryBell', 'scheduleNextBoundaryBell',
    '_clearPeriodClassesCache', '_hasPeriodClassesMap', '_normalizePeriodClassRow',
    '_cachePeriodClassesForIndex', '_getCachedPeriodClassesForIndex',
    'ingestPeriodClassesFromPayload', 'pickRuntimePeriodClasses',
  ].forEach(name => vm.runInContext(productionFunction(name), ctx, { filename: filename + ':' + name }));
  return { ctx, timers, ui, setTime: value => { clock = Date.parse(value); } };
}

const row = (index, teacher = 'Teacher ' + index) => ({
  period_index: index, class_name: 'Class ' + index, subject_name: 'Math', teacher_name: teacher,
});
function snapshot() {
  return {
    meta: { date: '2026-10-12', is_school_day: true },
    state: { type: 'period', index: 1, from: '08:00', to: '09:00' },
    current_period: { index: 1, from: '08:00', to: '09:00' },
    day_path: [
      { kind: 'period', index: 1, label: 'Period 1', from: '08:00', to: '09:00' },
      { kind: 'period', index: 2, label: 'Period 2', from: '09:00', to: '10:00' },
    ],
    period_classes_map: { '1': [row(1)], '2': [row(2)] },
    period_classes: [row(1)],
  };
}

test('authoritative empty timetable cancels old schedule, countdown and pending bell', () => {
  const { ctx, timers, ui, setTime } = harness();
  ctx.dayEngineLoad(snapshot());
  assert.ok(timers.has('bell_boundary'));
  ctx.rt.activeTargetHM = '09:00';
  ctx.rt.activeTargetMs = Date.parse('2026-10-12T09:00:00+03:00');
  ctx.dayEngineLoad({ day_path: [] });
  assert.equal(ctx._dayEngine.blocks.length, 0);
  assert.equal(ctx._dayEngine.ringPoints.length, 0);
  assert.equal(timers.has('bell_boundary'), false);
  assert.equal(ctx.hasActiveCountdown, false);
  assert.equal(ctx.countdownSeconds, null);
  assert.equal(ctx.rt.activeTargetMs, null);
  assert.equal(ctx.rt.activePeriodIndex, null);
  setTime('2026-10-12T09:00:00+03:00');
  assert.equal(ctx.dayEngineSyncToLocalNow(), false);
  assert.equal(ctx.dayEngineOnZero(), false);
  ctx.tickBoundaryBell();
  assert.equal(ui.bells, 0);
});

test('missing timetable in a legacy response preserves local transitions', () => {
  const { ctx, timers } = harness();
  ctx.dayEngineLoad(snapshot());
  const activeTimer = timers.get('bell_boundary');
  ctx.dayEngineLoad({ state: { type: 'period' } });
  assert.equal(ctx._dayEngine.blocks.length, 2);
  assert.equal(timers.get('bell_boundary'), activeTimer);
});

test('legacy response for a new day cannot retain yesterday local timetable', () => {
  const { ctx, setTime } = harness();
  ctx.dayEngineLoad(snapshot());
  setTime('2026-10-13T08:30:00+03:00');
  ctx.dayEngineLoad({ meta: { date: '2026-10-13' }, state: { type: 'period' } });
  assert.equal(ctx._dayEngine.blocks.length, 0);
  assert.equal(ctx.dayEngineSyncToLocalNow(), false);
});

for (const state of [
  { type: 'NO_SCHEDULE_TODAY' }, { type: 'AWAITING_SETUP' },
  { type: 'off', reason: 'holiday' }, { type: 'off', reason: 'offline_unknown_day' },
]) {
  test('server state has priority over local blocks: ' + JSON.stringify(state), () => {
    const { ctx, timers } = harness();
    ctx.dayEngineLoad(snapshot());
    const payload = { ...snapshot(), state };
    ctx.lastPayloadForFiltering = payload;
    ctx.dayEngineLoad(payload);
    assert.equal(ctx.dayEngineSyncToLocalNow(), false);
    assert.equal(ctx._dayEngine.blocks.length, 0);
    assert.equal(timers.has('bell_boundary'), false);
    assert.equal(ctx.pickRuntimePeriodClasses(payload).length, 0);
  });
}

test('identical hours on the following school day rebuild timestamps and remain active', () => {
  const { ctx, setTime } = harness();
  const payload = snapshot();
  ctx.dayEngineLoad(payload);
  const oldStart = ctx._dayEngine.blocks[0].fromMs;
  const oldSignature = ctx._dayEngine.lastDayKey;
  setTime('2026-10-13T08:30:00+03:00');
  payload.meta.date = '2026-10-13';
  ctx.dayEngineLoad(payload);
  assert.equal(ctx._dayEngine.blocks[0].fromMs - oldStart, 24 * 60 * 60 * 1000);
  assert.notEqual(ctx._dayEngine.lastDayKey, oldSignature);
  assert.equal(ctx.dayEngineFindNow().current.index, 1);
  assert.equal(ctx.dayEngineFindNow().dayOver, false);
});

test('school date rollover rebuilds undated legacy schedules independently of device timezone', () => {
  const { ctx, setTime } = harness();
  const payload = { day_path: snapshot().day_path };
  ctx.dayEngineLoad(payload);
  const before = ctx._dayEngine.blocks[0].fromMs;
  setTime('2026-10-13T08:30:00+03:00');
  ctx.dayEngineLoad(payload);
  assert.equal(ctx._dayEngine.blocks[0].fromMs - before, 86400000);
});

test('yesterday dated snapshot cannot drive today schedule or resurrect cached teachers', () => {
  const { ctx, setTime } = harness();
  const payload = snapshot();
  ctx.dayEngineLoad(payload);
  assert.equal(ctx.pickRuntimePeriodClasses(payload).length, 1);
  setTime('2026-10-13T08:30:00+03:00');
  ctx.dayEngineLoad(payload);
  assert.equal(ctx._dayEngine.blocks.length, 0);
  assert.equal(ctx.pickRuntimePeriodClasses(payload).length, 0);
});

test('local boundary advances period, rows and countdown with one bell without another snapshot', () => {
  const { ctx, ui, setTime, timers } = harness();
  const payload = snapshot();
  ctx.lastPayloadForFiltering = payload;
  ctx.dayEngineLoad(payload);
  assert.equal(ctx.pickRuntimePeriodClasses(payload)[0].teacher_name, 'Teacher 1');
  setTime('2026-10-12T09:00:00+03:00');
  timers.get('bell_boundary').callback();
  assert.equal(ctx.dayEngineSyncToLocalNow(), true);
  ctx.tickBoundaryBell();
  ctx.dayEngineSyncToLocalNow();
  assert.equal(ctx.rt.activePeriodIndex, 2);
  assert.equal(ctx.countdownSeconds, 3600);
  assert.equal(ctx.hasActiveCountdown, true);
  assert.equal(ui.rows[0].teacher_name, 'Teacher 2');
  assert.equal(ui.bells, 1);
  setTime('2026-10-12T10:00:00+03:00');
  timers.get('bell_boundary').callback();
  ctx.dayEngineOnZero();
  assert.equal(ctx.rt.activeStateType, 'after');
  assert.equal(ctx.hasActiveCountdown, false);
  assert.equal(ui.rows.length, 0);
  assert.equal(ui.bells, 2);
});

test('same-day identical snapshot does not re-arm or replay the boundary bell', () => {
  const { ctx, timers, ui } = harness();
  ctx.dayEngineLoad(snapshot());
  const timer = timers.get('bell_boundary');
  ctx.dayEngineLoad(snapshot());
  assert.equal(timers.get('bell_boundary'), timer);
  assert.equal(ui.bells, 0);
});

test('full empty assignment map removes the last teacher even with stale fallback rows', () => {
  const { ctx } = harness();
  const payload = snapshot();
  payload.day_path[0].teacher = 'Stale day-path teacher';
  payload.day_path[0].class = 'Old class';
  assert.equal(ctx.pickRuntimePeriodClasses(payload).length, 1);
  payload.period_classes_map = {};
  assert.equal(ctx.pickRuntimePeriodClasses(payload).length, 0);
  assert.equal(Object.keys(ctx.periodClassesByIndex).length, 0);
});

test('map replacement removes omitted period keys, and explicit empty rows stay empty', () => {
  const { ctx } = harness();
  const payload = snapshot();
  ctx.ingestPeriodClassesFromPayload(payload);
  payload.period_classes_map = { '1': [] };
  assert.equal(ctx.pickRuntimePeriodClasses(payload).length, 0);
  ctx.rt.activePeriodIndex = 2;
  assert.equal(ctx.pickRuntimePeriodClasses(payload).length, 0);
  assert.equal(Object.hasOwn(ctx.periodClassesByIndex, '2'), false);
});

test('legacy current-period list accepts deletion without restoring a day-path teacher', () => {
  const { ctx } = harness();
  const payload = snapshot();
  delete payload.period_classes_map;
  payload.day_path[0].teacher = 'Teacher from day path';
  payload.day_path[0].class = 'Class from day path';
  assert.equal(ctx.pickRuntimePeriodClasses(payload)[0].teacher_name, 'Teacher 1');
  payload.period_classes = [];
  assert.equal(ctx.pickRuntimePeriodClasses(payload).length, 0);
  assert.equal(ctx.pickRuntimePeriodClasses(payload).length, 0);
});

test('legacy single-row day-path assignments remain supported without a map or list', () => {
  const { ctx } = harness();
  const payload = snapshot();
  delete payload.period_classes_map;
  delete payload.period_classes;
  payload.day_path[0].teacher = 'Legacy teacher';
  payload.day_path[0].class = 'Legacy class';
  assert.equal(ctx.pickRuntimePeriodClasses(payload)[0].teacher_name, 'Legacy teacher');
});

test('legacy unindexed snapshot rows do not migrate into the next local period', () => {
  const { ctx } = harness();
  const payload = snapshot();
  delete payload.period_classes_map;
  delete payload.current_period.index;
  delete payload.state.index;
  payload.period_classes = [{ class_name: 'Unindexed class', teacher_name: 'Old teacher' }];
  assert.equal(ctx.pickRuntimePeriodClasses(payload).length, 1);
  Object.assign(ctx.rt, { activePeriodIndex: 2, activeFromHM: '09:00', activeToHM: '10:00' });
  assert.equal(ctx.pickRuntimePeriodClasses(payload).length, 0);
});

test('day and timetable changes clear legacy assignment history', () => {
  const { ctx, setTime } = harness();
  const payload = snapshot();
  delete payload.period_classes_map;
  ctx.ingestPeriodClassesFromPayload(payload);
  delete payload.period_classes;
  payload.day_path[0].from = '08:05';
  assert.equal(ctx.pickRuntimePeriodClasses(payload).length, 0);
  payload.period_classes = [row(1)];
  ctx.ingestPeriodClassesFromPayload(payload);
  setTime('2026-10-13T08:30:00+03:00');
  payload.meta.date = '2026-10-13';
  delete payload.period_classes;
  assert.equal(ctx.pickRuntimePeriodClasses(payload).length, 0);
});

function alertClockHarness() {
  const deviceTime = Date.parse('2026-10-12T08:20:00+03:00');
  const timers = new Map();
  function element() {
    const classes = new Set(['u-hidden']);
    return { classes, dataset: {}, setAttribute() {}, removeAttribute() {},
      classList: { add: value => classes.add(value), remove: value => classes.delete(value) } };
  }
  const overlay = element();
  const banner = element();
  const nodes = { emergencyOverlay: overlay, emergencySymbol: element(), emergencyKind: element() };
  const ctx = {
    Date: { now: () => deviceTime, parse: Date.parse },
    serverOffsetMs: 0, hasServerClockSync: true, clockDriftDetected: false,
    lastServerSyncAt: deviceTime, lastServerHeaderSyncAt: 0, lastLocalTime: deviceTime, lastCheckTime: deviceTime,
    annSource: [], annList: [], annPages: [], annLayoutKey: '', annPtr: 0, last: { annSig: '' },
    occasionCustomizations: {},
    emergencySource: [], emergencySelected: null, emergencySignature: '', emergencyPages: [], emergencyLayoutKey: '',
    document: { body: { getAttribute: () => '1' }, getElementById: id => nodes[id] || null },
    dom: { alertContainer: banner },
    safeText: value => String(value || ''), clamp: (n, a, b) => Math.max(a, Math.min(b, n)),
    screenFlag: () => true, setBoardData() {}, applyOccasionTheme() {}, refreshOccasionPresentation() {},
    scheduleNextBoundaryBell() {}, _log() {},
    toggleHidden: (el, hide) => hide ? el.classList.add('u-hidden') : el.classList.remove('u-hidden'),
    setTextIfChanged: (el, text) => { el.textContent = text; },
    clearNamedTimer: name => timers.delete(name),
    setNamedTimer: (name, callback, delay) => timers.set(name, { callback, delay }),
    // Keep presentation separate: use the real time filtering and timer logic.
    showAnnouncement() {
      banner.classList.remove('u-hidden');
      timers.set('announcement_rotate', { currentPage: 2 });
    },
    showEmergencyPage() {
      ctx.emergencyPages = [{ title: 'Visible page' }];
      timers.set('emergency_rotate', { currentPage: 2 });
    },
    refreshEmergencyLayout() {},
  };
  vm.createContext(ctx);
  ['nowMs', 'applyServerNowMs', 'annSignature', 'renderAnnouncements', 'renderEmergencyAlerts']
    .forEach(name => vm.runInContext(productionFunction(name), ctx, { filename: filename + ':' + name }));
  function loadWindow(starts, expires) {
    const item = { id: 1, title: 'Timed message', starts_at: starts, expires_at: expires };
    ctx.renderAnnouncements([item]);
    ctx.renderEmergencyAlerts([item]);
  }
  return { ctx, deviceTime, timers, banner, overlay, loadWindow };
}

test('clock correction immediately expires ordinary and emergency alerts without a new snapshot', () => {
  const { ctx, timers, banner, overlay, loadWindow } = alertClockHarness();
  loadWindow('2026-10-12T08:00:00+03:00', '2026-10-12T08:25:00+03:00');
  assert.equal(banner.classes.has('u-hidden'), false);
  assert.equal(overlay.classes.has('u-hidden'), false);
  ctx.applyServerNowMs(Date.parse('2026-10-12T08:30:00+03:00'), 'header');
  assert.equal(banner.classes.has('u-hidden'), true);
  assert.equal(overlay.classes.has('u-hidden'), true);
  for (const name of ['announcement_boundary', 'emergency_boundary', 'announcement_rotate', 'emergency_rotate']) {
    assert.equal(timers.has(name), false, name);
  }
});

test('clock correction re-arms future expiry without restarting the current alert pages', () => {
  const { ctx, timers, loadWindow } = alertClockHarness();
  loadWindow('2026-10-12T08:00:00+03:00', '2026-10-12T08:25:00+03:00');
  const normalRotation = timers.get('announcement_rotate');
  const emergencyRotation = timers.get('emergency_rotate');
  ctx.applyServerNowMs(Date.parse('2026-10-12T08:24:00+03:00'), 'header');
  assert.equal(timers.get('announcement_boundary').delay, 60050);
  assert.equal(timers.get('emergency_boundary').delay, 60050);
  assert.equal(timers.get('announcement_rotate'), normalRotation);
  assert.equal(timers.get('emergency_rotate'), emergencyRotation);
});

test('sub-second clock jitter leaves alert boundary and rotation timers untouched', () => {
  const { ctx, deviceTime, timers, loadWindow } = alertClockHarness();
  loadWindow('2026-10-12T08:00:00+03:00', '2026-10-12T08:25:00+03:00');
  const original = new Map(timers);
  for (let step = 1; step <= 10; step++) ctx.applyServerNowMs(deviceTime + step * 40, 'header');
  for (const [name, timer] of original) assert.equal(timers.get(name), timer, name);
});

test('backward clock correction hides alerts whose start time is now in the future', () => {
  const { ctx, timers, banner, overlay, loadWindow } = alertClockHarness();
  loadWindow('2026-10-12T08:15:00+03:00', '2026-10-12T08:25:00+03:00');
  ctx.applyServerNowMs(Date.parse('2026-10-12T08:14:00+03:00'), 'header');
  assert.equal(banner.classes.has('u-hidden'), true);
  assert.equal(overlay.classes.has('u-hidden'), true);
  assert.equal(timers.get('announcement_boundary').delay, 60050);
  assert.equal(timers.get('emergency_boundary').delay, 60050);
});

test('forward clock correction reveals newly started alerts without waiting for the old timer', () => {
  const { ctx, timers, banner, overlay, loadWindow } = alertClockHarness();
  loadWindow('2026-10-12T08:25:00+03:00', '2026-10-12T08:35:00+03:00');
  assert.equal(banner.classes.has('u-hidden'), true);
  assert.equal(overlay.classes.has('u-hidden'), true);
  ctx.applyServerNowMs(Date.parse('2026-10-12T08:30:00+03:00'), 'header');
  assert.equal(banner.classes.has('u-hidden'), false);
  assert.equal(overlay.classes.has('u-hidden'), false);
  assert.equal(timers.get('announcement_boundary').delay, 300050);
  assert.equal(timers.get('emergency_boundary').delay, 300050);
});
