import test from 'node:test';
import assert from 'node:assert/strict';
import worker, { checkAndRecover, feedsDue, cycleKey } from '../external/recovery-worker.mjs';

const SITE = 'https://edgelab-sports.jkv9c8bzjn.chatgpt.site';
const REPOSITORY = '98vkr5495w-boop/Securus-scheduler';

const NOW = Date.parse('2026-09-10T04:22:00Z');
const SECRET = 'test-only-never-a-real-credential';
const iso = age => new Date(NOW - age * 60000).toISOString();
function sources(age = 0) {
  return { sources: ['mlb-stats-api', 'action-network', 'sleeper-nfl', 'kalshi', 'open-meteo', 'nflverse', 'baseball-savant']
    .map(id => ({ id, lastRun: { status: 'SUCCEEDED', completedAt: iso(age) } })),
  paperBetReadiness: { checkedAt: iso(0) },
  lastPaperScan: { mode: 'PAPER_ONLY', runId: 'test-journal-receipt', completedAt: iso(0) },
  storage: { capacity: { capacityState: 'NORMAL', utilizationPercent: 60,
    actualBytes: 314572800, sizeSource: 'D1_META', measuredAt: iso(0),
    archives: { lastMaintenance: { status: 'SUCCEEDED', triggerName: 'cloudflare-cron-10m',
      startedAt: iso(22), completedAt: iso(21) } } } } };
}
function fixture(options = {}) {
  const calls = [];
  const payload = options.payload ?? sources(31);
  const runs = options.runs ?? [{ id: 42, head_branch: 'main', event: 'schedule', status: 'completed', created_at: iso(60) }];
  const jobs = options.jobs ?? [{ name: 'collect-inputs', conclusion: 'success', started_at: iso(59) }];
  const fetch = async (url, init) => {
    calls.push({ url, ...init });
    if (options.respond) {
      const response = options.respond(url, init);
      if (response) return response;
    }
    if (url === `${SITE}/api/data-sources`) return Response.json(payload);
    if (init.method === 'POST') return new Response(null, { status: 204 });
    if (url.includes('/jobs?')) return Response.json({ jobs });
    return Response.json({ workflow_runs: runs });
  };
  return { calls, execute: (env = { GITHUB_RECOVERY_TOKEN: SECRET }) => checkAndRecover(env, { fetch, now: () => NOW }) };
}

test('fresh feeds suppress writes even when betting policy abstains', async () => {
  const payload = sources(); payload.paperBetReadiness.status = 'NO_BET';
  const f = fixture({ payload });
  assert.equal((await f.execute()).status, 'FEEDS_CURRENT');
  assert.equal(f.calls.length, 1);
});

test('refresh boundary, failed, missing, future and timezone-less records', () => {
  assert.equal(feedsDue(sources(29), NOW), false);
  for (const age of [30, 180, -1]) assert.equal(feedsDue(sources(age), NOW), true);
  for (const run of [null, { status: 'FAILED', completedAt: iso(0) },
    { status: 'SUCCEEDED', completedAt: '2026-09-10T04:22:00' }]) {
    const payload = sources(); payload.sources[0].lastRun = run;
    assert.equal(feedsDue(payload, NOW), true);
  }
});

test('overdue deep statistics are refreshed despite fresh frequent sources', async () => {
  const payload = sources(); payload.sources.find(x => x.id === 'nflverse').lastRun.completedAt = iso(360);
  assert.equal((await fixture({ payload }).execute()).status, 'RECOVERY_DISPATCHED');
});

test('missed GitHub cron dispatches only the trusted current canonical cycle', async () => {
  const f = fixture({ payload: sources(180) });
  assert.deepEqual(await f.execute(), { status: 'RECOVERY_DISPATCHED', cycle: '2026-09-10T04:07:00Z' });
  const posts = f.calls.filter(x => x.method === 'POST');
  assert.equal(posts.length, 1);
  assert.equal(posts[0].url, `https://api.github.com/repos/${REPOSITORY}/actions/workflows/securus-scheduler.yml/dispatches`);
  assert.deepEqual(JSON.parse(posts[0].body), { ref: 'main', inputs: { recovery: 'true', cycle_key: '2026-09-10T04:07:00Z' } });
  assert.equal(cycleKey(Date.parse('2026-09-10T04:38:00Z')), '2026-09-10T04:37:00Z');
});

test('GitHub credentials never reach Site and redirects are disabled', async () => {
  const f = fixture(); await f.execute();
  assert.equal(f.calls[0].headers.Authorization, undefined);
  for (const call of f.calls) {
    assert.equal(call.redirect, 'manual');
    if (call.headers.Authorization) assert.ok(call.url.startsWith(`https://api.github.com/repos/${REPOSITORY}/`));
  }
});

test('Site and GitHub redirects fail without following Location or dispatching', async () => {
  for (const origin of [SITE, 'https://api.github.com']) {
    for (const status of [301, 302, 303, 307, 308]) {
      const f = fixture({ respond: (url) => url.startsWith(origin)
        ? new Response(null, { status, headers: { Location: 'https://untrusted.example/' } }) : null });
      await assert.rejects(f.execute(), new RegExp(`HTTP_${status}`));
      assert.equal(f.calls.length, origin === SITE ? 1 : 2);
      assert.equal(f.calls.filter(x => x.method === 'POST').length, 0);
      assert.ok(f.calls.every(x => !x.url.includes('untrusted.example')));
    }
  }
});

test('pending runtime prevents duplicate dispatch', async () => {
  for (const status of ['queued', 'in_progress', 'waiting']) {
    const f = fixture({ runs: [{ id: 42, head_branch: 'main', event: 'workflow_dispatch', status, created_at: iso(60) }] });
    assert.equal((await f.execute()).status, 'RUNTIME_ALREADY_PENDING');
    assert.equal(f.calls.filter(x => x.method === 'POST').length, 0);
  }
});

test('recent collection and recent dispatch receipt bound retry races', async () => {
  const collect = fixture({ jobs: [{ name: 'collect-inputs', conclusion: 'failure', started_at: iso(9) }] });
  assert.equal((await collect.execute()).status, 'RECOVERY_COOLDOWN');
  const dispatch = fixture({ runs: [{ id: 42, head_branch: 'main', event: 'workflow_dispatch', status: 'completed', created_at: iso(9) }] });
  assert.equal((await dispatch.execute()).status, 'RECOVERY_COOLDOWN');
});

test('successful scan-only reruns cannot reset the collection clock', async () => {
  const f = fixture({ jobs: [{ name: 'collect-and-scan', conclusion: 'success', started_at: iso(1) },
    { name: 'collect-inputs', conclusion: 'skipped', started_at: iso(0) }] });
  assert.equal((await f.execute()).status, 'RECOVERY_DISPATCHED');
});

test('critical and unknown storage never start collection', async () => {
  for (const capacity of [{ capacityState: 'CRITICAL', utilizationPercent: 80 },
    { capacityState: 'WARNING', utilizationPercent: 85 }, {}, { capacityState: 'NORMAL' }]) {
    const payload = sources(180); payload.storage.capacity = capacity;
    const f = fixture({ payload });
    await assert.rejects(f.execute(), /STORAGE_/);
    assert.equal(f.calls.length, 1);
  }
});

test('unavailable, oversized and malformed responses fail without a dispatch', async () => {
  const variants = [() => new Response('private response must not be logged', { status: 503 }),
    () => new Response('html response', { headers: { 'content-type': 'text/html' } }),
    () => new Response('x'.repeat(524289), { headers: { 'content-type': 'application/json' } }),
    () => Response.json({ unexpected: true })];
  for (const respond of variants) {
    const f = fixture({ respond });
    await assert.rejects(f.execute(), error => !error.message.includes('private response'));
    assert.equal(f.calls.filter(x => x.method === 'POST').length, 0);
  }
});

test('failed or ambiguous dispatch is attempted only once', async () => {
  for (const status of [403, 429, 500, 200, 302]) {
    const f = fixture({ respond: (_url, init) => init.method === 'POST' ? new Response(null, { status }) : null });
    await assert.rejects(f.execute());
    assert.equal(f.calls.filter(x => x.method === 'POST').length, 1);
  }
});

test('new GitHub dispatch response requires a valid run receipt', async () => {
  const f = fixture({ respond: (_url, init) => init.method === 'POST'
    ? Response.json({ workflow_run_id: 123, run_url: 'https://untrusted.example/' }) : null });
  assert.equal((await f.execute()).status, 'RECOVERY_DISPATCHED');
  assert.equal(f.calls.filter(x => x.method === 'POST').length, 1);
  assert.ok(f.calls.every(x => !x.url.includes('untrusted.example')));
  await assert.rejects(fixture({ runs: [{}] }).execute(), /HISTORY_UNAVAILABLE/);
});

test('missing secret and invalid collection history cannot claim success', async () => {
  await assert.rejects(fixture().execute({}), /SECRET_MISSING/);
  const f = fixture({ jobs: [{ name: 'collect-inputs', conclusion: 'failure', started_at: 'invalid' }] });
  await assert.rejects(f.execute(), /TIMESTAMP_UNAVAILABLE/);
});

test('public HTTP calls cannot initiate any authenticated action', async () => {
  for (const method of ['POST', 'PUT', 'DELETE']) {
    assert.equal((await worker.fetch(new Request('https://timer.example/', { method }))).status, 405);
  }
  const response = await worker.fetch(new Request('https://timer.example/'));
  assert.equal((await response.json()).delivery, 'UNVERIFIED_BY_HTTP');
  assert.equal((await worker.fetch(new Request('https://timer.example/run'))).status, 404);
});

test('fresh feed data cannot hide a missing recovery credential', async () => {
  const f = fixture({ payload: sources(0) });
  await assert.rejects(f.execute({}), /SECRET_MISSING/);
  assert.equal(f.calls.filter(x => x.method === 'POST').length, 0);
});

test('fresh feeds cannot hide a missing or overdue completed paper scan', async () => {
  for (const scan of [null, { mode: 'PAPER_ONLY', runId: 'old', completedAt: iso(61) }]) {
    const payload = sources(0); payload.lastPaperScan = scan;
    assert.equal((await fixture({ payload }).execute()).status, 'RECOVERY_DISPATCHED');
  }
});

test('unknown, stale, future, or unmeasured capacity cannot authorize dispatch', async () => {
  for (const changes of [
    { measuredAt: iso(16) }, { measuredAt: iso(-1) },
    { measuredAt: null, sizeSource: 'UNKNOWN' }, { actualBytes: 0 },
    { maintenanceState: 'CRITICAL' },
  ]) {
    const payload = sources(31); Object.assign(payload.storage.capacity, changes);
    const f = fixture({ payload });
    await assert.rejects(f.execute(), /STORAGE_/);
    assert.equal(f.calls.filter(x => x.method === 'POST').length, 0);
  }
});

test('unfinished collector receipts block external recovery regardless of receipt age', async () => {
  for (const status of ['RUNNING', 'QUEUED', 'IN_PROGRESS']) {
    for (const age of [2, 120]) {
      const payload = sources(31);
      payload.sources[0].lastRun = { status, startedAt: iso(age) };
      const f = fixture({ payload });
      assert.equal((await f.execute()).status, 'COLLECTION_ALREADY_PENDING');
      assert.equal(f.calls.filter(x => x.method === 'POST').length, 0);
    }
  }
});

test('recent or unfinished independent maintenance prevents overlapping recovery', async () => {
  for (const receipt of [
    { status: 'SUCCEEDED', startedAt: iso(11), completedAt: iso(9) },
    { status: 'RUNNING', startedAt: iso(20), completedAt: null },
  ]) {
    const payload = sources(31); payload.storage.capacity.archives.lastMaintenance = receipt;
    const f = fixture({ payload });
    assert.match((await f.execute()).status, /MAINTENANCE_ALREADY_PENDING|RECOVERY_COOLDOWN/);
    assert.equal(f.calls.filter(x => x.method === 'POST').length, 0);
  }
});

test('scheduler maintenance and incomplete job pages cannot be ignored', async () => {
  const maintenance = fixture({ jobs: [{ name: 'maintain-storage', conclusion: 'success',
    started_at: iso(9) }] });
  assert.equal((await maintenance.execute()).status, 'RECOVERY_COOLDOWN');
  const incomplete = fixture({ respond: url => url.includes('/jobs?')
    ? Response.json({ total_count: 101, jobs: [] }) : null });
  await assert.rejects(incomplete.execute(), /HISTORY_UNAVAILABLE/);
  assert.equal(incomplete.calls.filter(x => x.method === 'POST').length, 0);
});

test('empty history is unknown, not evidence of an idle runtime', async () => {
  const f = fixture({ runs: [] });
  await assert.rejects(f.execute(), /HISTORY_UNAVAILABLE/);
  assert.equal(f.calls.filter(x => x.method === 'POST').length, 0);
});

test('quick snapshots use their fresh D1 probe, not an old maintenance growth sample', async () => {
  const payload = sources(31); delete payload.storage.capacity.measuredAt;
  payload.storage.capacity.maintenanceMeasuredAt = iso(180);
  assert.equal((await fixture({ payload }).execute()).status, 'RECOVERY_DISPATCHED');
  payload.paperBetReadiness.checkedAt = iso(16);
  await assert.rejects(fixture({ payload }).execute(), /STORAGE_MEASUREMENT_STALE/);
  payload.paperBetReadiness.checkedAt = iso(0);
  payload.paperBetReadiness.snapshotFreshness = 'STALE';
  await assert.rejects(fixture({ payload }).execute(), /STORAGE_MEASUREMENT_STALE/);
});

test('Site-projected FAILED abandonment does not count as running work', async () => {
  const payload = sources(31);
  payload.sources[0].lastRun = { status: 'FAILED', startedAt: iso(90), completedAt: iso(70) };
  assert.equal((await fixture({ payload }).execute()).status, 'RECOVERY_DISPATCHED');
});

test('unknown or future maintenance evidence fails closed and exact cooldown boundary recovers', async () => {
  for (const receipt of [null, {},
    { status: 'SUCCEEDED', startedAt: iso(20), completedAt: iso(-1) },
    { status: 'FAILED', startedAt: iso(20), completedAt: null }]) {
    const payload = sources(31); payload.storage.capacity.archives.lastMaintenance = receipt;
    const f = fixture({ payload });
    await assert.rejects(f.execute(), /MAINTENANCE_/);
    assert.equal(f.calls.filter(x => x.method === 'POST').length, 0);
  }
  const payload = sources(31);
  payload.storage.capacity.archives.lastMaintenance.completedAt = iso(10);
  assert.equal((await fixture({ payload }).execute()).status, 'RECOVERY_DISPATCHED');
});

test('maintenance or collection starting during history inspection suppresses dispatch', async () => {
  for (const race of ['collection', 'maintenance', 'fresh', 'critical']) {
    let reads = 0;
    const payload = sources(31);
    if (race === 'collection') payload.sources[0].lastRun.status = 'RUNNING';
    if (race === 'maintenance') payload.storage.capacity.archives.lastMaintenance.completedAt = iso(0);
    if (race === 'critical') payload.storage.capacity.utilizationPercent = 85;
    const f = fixture({ respond: url => {
      if (url === `${SITE}/api/data-sources` && ++reads === 2) {
        return Response.json(race === 'fresh' ? sources(0) : payload);
      }
      return null;
    } });
    if (race === 'critical') await assert.rejects(f.execute(), /STORAGE_CRITICAL/);
    else assert.notEqual((await f.execute()).status, 'RECOVERY_DISPATCHED');
    assert.equal(f.calls.filter(x => x.method === 'POST').length, 0);
  }
});

test('unknown run status, foreign-only history, and malformed job totals fail closed', async () => {
  const base = { id: 42, head_branch: 'main', event: 'schedule', status: 'completed', created_at: iso(60) };
  for (const runs of [[{ ...base, status: 'unknown' }], [{ ...base, head_branch: 'untrusted' }]]) {
    const f = fixture({ runs });
    await assert.rejects(f.execute(), /HISTORY_UNAVAILABLE/);
    assert.equal(f.calls.filter(x => x.method === 'POST').length, 0);
  }
  for (const total_count of [-1, '1', 0.5]) {
    const f = fixture({ respond: url => url.includes('/jobs?')
      ? Response.json({ total_count, jobs: [] }) : null });
    await assert.rejects(f.execute(), /HISTORY_UNAVAILABLE/);
  }
});

test('maximum history stays inside its request budget with one final evidence read and one POST', async () => {
  const runs = Array.from({ length: 20 }, (_, index) => ({ id: index + 1,
    head_branch: 'main', event: 'schedule', status: 'completed', created_at: iso(60 + index) }));
  const f = fixture({ runs });
  assert.equal((await f.execute()).status, 'RECOVERY_DISPATCHED');
  assert.equal(f.calls.length, 24);
  assert.equal(f.calls.filter(x => x.method === 'POST').length, 1);
});
