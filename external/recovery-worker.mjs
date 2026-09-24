// Independent timer transport only. It cannot directly collect or place bets.
const SITE = 'https://edgelab-sports.jkv9c8bzjn.chatgpt.site';
const REPOSITORY = '98vkr5495w-boop/Securus-scheduler';
const WORKFLOW = 'securus-scheduler.yml';
const API = 'https://api.github.com';
const VERSION = 'securus-external-recovery-20260924.1';
const FREQUENT = ['mlb-stats-api', 'action-network', 'sleeper-nfl', 'kalshi', 'open-meteo'];
const DEEP = ['nflverse', 'baseball-savant'];
const MINUTE = 60000;
const HEADERS = { 'cache-control': 'no-store', 'x-content-type-options': 'nosniff' };
const UNFINISHED = new Set(['RUNNING', 'QUEUED', 'IN_PROGRESS']);

function timestamp(value) {
  if (typeof value !== 'string' || !/(Z|[+-]\d\d:\d\d)$/.test(value)) return NaN;
  return Date.parse(value);
}

export function cycleKey(now) {
  return new Date(Math.floor((now - 7 * MINUTE) / (30 * MINUTE)) * 30 * MINUTE + 7 * MINUTE)
    .toISOString().replace('.000Z', 'Z');
}

export function feedsDue(payload, now) {
  if (!Array.isArray(payload?.sources)) return true;
  const rows = new Map(payload.sources.filter(row => row && typeof row === 'object')
    .map(row => [row.id, row.lastRun]));
  return [...FREQUENT.map(id => [id, 30]), ...DEEP.map(id => [id, 360])].some(([id, minutes]) => {
    const run = rows.get(id);
    const age = now - timestamp(run?.completedAt);
    return run?.status !== 'SUCCEEDED' || !Number.isFinite(age) || age < 0 || age >= minutes * MINUTE;
  });
}

function scanDue(payload, now) {
  const scan = payload?.lastPaperScan;
  const age = now - timestamp(scan?.completedAt);
  return scan?.mode !== 'PAPER_ONLY' || typeof scan?.runId !== 'string' || !scan.runId ||
    !Number.isFinite(age) || age < 0 || age >= 60 * MINUTE;
}

function recoveryGate(payload, now) {
  const capacity = payload?.storage?.capacity;
  if (!['NORMAL', 'WARNING', 'CRITICAL'].includes(capacity?.capacityState) ||
      !Number.isFinite(capacity?.utilizationPercent) || capacity.utilizationPercent < 0 ||
      !Number.isFinite(capacity?.actualBytes) || capacity.actualBytes <= 0 ||
      capacity.sizeSource !== 'D1_META') throw new Error('STORAGE_STATUS_UNAVAILABLE');
  if (capacity.capacityState === 'CRITICAL' || capacity.maintenanceState === 'CRITICAL' ||
      capacity.utilizationPercent >= 85) throw new Error('STORAGE_CRITICAL_REQUIRES_MAINTENANCE');

  // Full snapshots timestamp their D1 measurement directly. The quick Site
  // snapshot performs a new D1 metadata probe with its checkedAt timestamp;
  // maintenanceMeasuredAt belongs to older growth/retention evidence, not it.
  const measuredAt = capacity.measuredAt === undefined
    ? payload.paperBetReadiness?.checkedAt : capacity.measuredAt;
  const age = now - timestamp(measuredAt);
  if (!Number.isFinite(age) || age < 0 || age > 15 * MINUTE ||
      payload.paperBetReadiness?.snapshotFreshness === 'STALE') {
    throw new Error('STORAGE_MEASUREMENT_STALE');
  }
  if (!Array.isArray(payload.sources)) throw new Error('COLLECTION_HISTORY_UNAVAILABLE');
  // Only the Site can project an expired lease/journal to FAILED. Never infer
  // abandonment here or delete a lock because its visible receipt looks old.
  if (payload.sources.some(row => [...FREQUENT, ...DEEP].includes(row?.id) &&
      UNFINISHED.has(row?.lastRun?.status))) return { status: 'COLLECTION_ALREADY_PENDING' };

  if (!feedsDue(payload, now) && !scanDue(payload, now)) return { status: 'FEEDS_CURRENT' };
  const maintenance = capacity.archives?.lastMaintenance;
  if (!maintenance || typeof maintenance !== 'object') throw new Error('MAINTENANCE_HISTORY_UNAVAILABLE');
  if (UNFINISHED.has(maintenance.status)) return { status: 'MAINTENANCE_ALREADY_PENDING' };
  if (!['SUCCEEDED', 'FAILED'].includes(maintenance.status)) throw new Error('MAINTENANCE_HISTORY_UNAVAILABLE');
  const started = timestamp(maintenance.startedAt);
  const completed = timestamp(maintenance.completedAt);
  if (!Number.isFinite(started) || !Number.isFinite(completed) || completed < started || completed > now) {
    throw new Error('MAINTENANCE_TIMESTAMP_UNAVAILABLE');
  }
  if (now - completed < 10 * MINUTE) return { status: 'RECOVERY_COOLDOWN' };
  return null;
}

async function boundedJson(response) {
  if (!response.headers.get('content-type')?.toLowerCase().includes('application/json')) {
    await response.body?.cancel();
    throw new Error('NON_JSON_RESPONSE');
  }
  const reader = response.body?.getReader();
  if (!reader) throw new Error('EMPTY_RESPONSE');
  const chunks = []; let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 524288) { await reader.cancel(); throw new Error('RESPONSE_TOO_LARGE'); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  try {
    const payload = JSON.parse(new TextDecoder().decode(bytes));
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error();
    return payload;
  } catch { throw new Error('INVALID_JSON_RESPONSE'); }
}

export async function checkAndRecover(env, io = {}) {
  const fetcher = io.fetch ?? fetch;
  const clock = io.now ?? Date.now;
  const now = clock();
  let requests = 0;
  async function request(url, method = 'GET', body) {
    if (++requests > 24 || clock() - now >= 90000) throw new Error('CHECK_BUDGET_EXCEEDED');
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), Math.min(10000, 90000 - (clock() - now)));
    const github = url.startsWith(`${API}/repos/${REPOSITORY}/`);
    // Both origins and every path are fixed here. Never follow returned URLs.
    if (!github && url !== `${SITE}/api/data-sources`) throw new Error('UNEXPECTED_DESTINATION');
    try {
      const response = await fetcher(url, {
        // Older Workers runtimes reject redirect: 'error' before making a
        // request. Manual mode is supported and 3xx responses fail below.
        method, redirect: 'manual', signal: controller.signal,
        headers: {
          Accept: 'application/json', 'User-Agent': VERSION,
          ...(github ? { Authorization: `Bearer ${env.GITHUB_RECOVERY_TOKEN}`,
            'X-GitHub-Api-Version': '2022-11-28' } : {}),
          ...(body ? { 'Content-Type': 'application/json' } : {}),
        },
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      if (!response.ok || response.status >= 300) {
        await response.body?.cancel();
        throw new Error(github ? `GITHUB_HTTP_${response.status}` : `SITE_HTTP_${response.status}`);
      }
      if (method === 'POST') {
        if (response.status === 200) {
          const receipt = await boundedJson(response);
          if (!Number.isSafeInteger(receipt.workflow_run_id) || receipt.workflow_run_id <= 0) {
            throw new Error('DISPATCH_NOT_CONFIRMED');
          }
          return null;
        }
        await response.body?.cancel();
        if (response.status !== 204) throw new Error('DISPATCH_NOT_CONFIRMED');
        return null;
      }
      return await boundedJson(response);
    } catch (error) {
      if (error?.name === 'AbortError' || error instanceof TypeError) throw new Error('REQUEST_UNAVAILABLE');
      throw error;
    } finally { clearTimeout(timer); }
  }

  // Fresh feeds must not make an unconfigured recovery deployment look ready.
  if (typeof env.GITHUB_RECOVERY_TOKEN !== 'string' || !env.GITHUB_RECOVERY_TOKEN.trim()) {
    throw new Error('GITHUB_RECOVERY_SECRET_MISSING');
  }
  const sources = await request(`${SITE}/api/data-sources`);
  const initialGate = recoveryGate(sources, clock());
  if (initialGate) return initialGate;

  const prefix = `${API}/repos/${REPOSITORY}`;
  const history = await request(`${prefix}/actions/workflows/${WORKFLOW}/runs?branch=main&per_page=20`);
  if (!Array.isArray(history.workflow_runs) || !history.workflow_runs.length) {
    throw new Error('COLLECTION_HISTORY_UNAVAILABLE');
  }
  let latestAttempt = -Infinity;
  let trustedRuns = 0;
  for (const run of history.workflow_runs) {
    if (!Number.isSafeInteger(run?.id) || run.id <= 0 || typeof run.head_branch !== 'string' ||
        typeof run.event !== 'string' || typeof run.status !== 'string') throw new Error('COLLECTION_HISTORY_UNAVAILABLE');
    if (run.head_branch !== 'main' || !['schedule', 'workflow_dispatch'].includes(run.event)) continue;
    trustedRuns++;
    if (!['completed', 'queued', 'in_progress', 'waiting', 'pending', 'requested'].includes(run.status)) {
      throw new Error('COLLECTION_HISTORY_UNAVAILABLE');
    }
    if (run.status !== 'completed') return { status: 'RUNTIME_ALREADY_PENDING' };
    // A just-accepted dispatch may not yet expose its jobs; bound that race too.
    const dispatchedAt = timestamp(run.created_at);
    if (!Number.isFinite(dispatchedAt)) throw new Error('COLLECTION_TIMESTAMP_UNAVAILABLE');
    if (dispatchedAt > now) throw new Error('FUTURE_RUN_TIMESTAMP');
    if (now - dispatchedAt < 10 * MINUTE) return { status: 'RECOVERY_COOLDOWN' };
    const jobs = await request(`${prefix}/actions/runs/${run.id}/jobs?filter=latest&per_page=100`);
    if (!Array.isArray(jobs.jobs) || (jobs.total_count !== undefined &&
        (!Number.isSafeInteger(jobs.total_count) || jobs.total_count !== jobs.jobs.length))) {
      throw new Error('COLLECTION_HISTORY_UNAVAILABLE');
    }
    for (const job of jobs.jobs) {
      if (!['collect-inputs', 'maintain-storage'].includes(job?.name) || job.conclusion === 'skipped') continue;
      const started = timestamp(job.started_at);
      if (!Number.isFinite(started) || started > now) throw new Error('COLLECTION_TIMESTAMP_UNAVAILABLE');
      latestAttempt = Math.max(latestAttempt, started);
    }
  }
  if (!trustedRuns) throw new Error('COLLECTION_HISTORY_UNAVAILABLE');
  if (now - latestAttempt < 10 * MINUTE) return { status: 'RECOVERY_COOLDOWN' };
  // Close the source/maintenance race during history reads. The scheduler's
  // shared concurrency and Site leases remain the final atomic guards.
  const finalGate = recoveryGate(await request(`${SITE}/api/data-sources`), clock());
  if (finalGate) return finalGate;
  const cycle = cycleKey(now);
  await request(`${prefix}/actions/workflows/${WORKFLOW}/dispatches`, 'POST', {
    ref: 'main', inputs: { recovery: 'true', cycle_key: cycle },
  });
  return { status: 'RECOVERY_DISPATCHED', cycle };
}

export default {
  async fetch(request) {
    if (request.method !== 'GET') return Response.json({ error: 'Read-only endpoint' }, { status: 405, headers: HEADERS });
    if (new URL(request.url).pathname !== '/') return Response.json({ error: 'Not found' }, { status: 404, headers: HEADERS });
    return Response.json({ version: VERSION, purpose: 'Independent scheduler recovery',
      delivery: 'UNVERIFIED_BY_HTTP', paperOnly: true }, { headers: HEADERS });
  },
  async scheduled(_event, env) {
    try {
      const result = await checkAndRecover(env);
      console.log(JSON.stringify({ event: 'securus-recovery-check', ...result }));
    } catch (error) {
      const code = /^[A-Z0-9_]+$/.test(error?.message ?? '') ? error.message : 'RECOVERY_CHECK_FAILED';
      console.error(JSON.stringify({ event: 'securus-recovery-error', code }));
      throw new Error(code);
    }
  },
};
