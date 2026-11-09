export class ApiError extends Error {
  constructor(message, status = 0, detail = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

const terminal = (state) => ['done', 'failed', 'stopped', 'error'].includes(state);
const jobPath = (id) => `/api/jobs/${encodeURIComponent(id)}`;
// Said when a request can't reach Digger, for the way the last Digger this page saw was started.
const UNREACHABLE = Object.freeze({
  start: "Can't reach Digger. Run cosine-digger start in Terminal.",
  serve: "Can't reach Digger. Start it again the way you started it.",
});

export function createApi({fetch = globalThis.fetch.bind(globalThis), EventSource = globalThis.EventSource} = {}) {
  let launcher = 'start';
  async function request(method, path, body, {signal, headers = {}} = {}) {
    const init = {method, signal, headers: {Accept: 'application/json', ...headers}};
    if (!['GET', 'HEAD'].includes(method.toUpperCase())) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body ?? {});
    }
    let response, raw;
    try {
      response = await fetch(path, init);
      raw = await response.text();
    } catch (error) {
      if (error.name === 'AbortError') throw error;
      throw new ApiError(UNREACHABLE[launcher]);
    }
    let data = null, valid = true;
    try { data = raw ? JSON.parse(raw) : null; } catch { valid = false; }
    if (!response.ok) {
      const detail = data?.detail ?? data?.error;
      const message = typeof detail === 'string' ? detail : detail?.message;
      throw new ApiError(message || `The server answered HTTP ${response.status}`, response.status, detail);
    }
    if (!valid) throw new ApiError('The local server returned invalid JSON.', response.status);
    if (path === '/api/health' && Object.hasOwn(UNREACHABLE, data?.launcher ?? '')) launcher = data.launcher;
    return data;
  }

  function followStepJob(jobId, {after = 0, onEvent = () => {}, onDisconnect = () => {}, pollOnly = false} = {}) {
    let cursor = after, source = null, timer = null, closed = false, reconnected = false;
    const controller = new AbortController();
    const close = () => {
      closed = true;
      source?.close();
      clearTimeout(timer);
      controller.abort();
    };
    function deliver(name, message) {
      if (closed || message.job_id !== jobId || !Number.isSafeInteger(message.seq) || message.seq <= cursor) return;
      if (message.seq !== cursor + 1) {
        disconnect(new ApiError('The event stream skipped a saved event.'));
        return;
      }
      cursor = message.seq;
      if (name === 'done' || name === 'error') close();
      onEvent({...message, event: name});
    }
    async function poll() {
      if (closed) return;
      try {
        const job = await request('GET', jobPath(jobId), undefined, {signal: controller.signal});
        if (closed) return;
        if (job.step_id) {
          const envelope = await request('GET', `/api/steps/${encodeURIComponent(job.step_id)}`, undefined,
            {signal: controller.signal});
          if (closed) return;
          // A snapshot and its cursor are one server transaction, not invented SSE events.
          if (envelope.job.id === jobId) {
            cursor = Math.max(cursor, envelope.job.cursor);
            onEvent({event: 'snapshot', job_id: jobId, envelope});
            if (terminal(envelope.job.state)) return close();
          } else if (terminal(job.state)) {
            return close(); // The step has moved to a different attempt.
          }
        } else {
          cursor = Math.max(cursor, job.last_seq);
          onEvent({event: 'job', job_id: jobId, job});
          if (terminal(job.state)) return close();
        }
      } catch (error) {
        if (closed) return;
        if (error.status === 404) {
          close();
          onDisconnect(error);
          return;
        }
        onDisconnect(error);
      }
      if (!closed) timer = setTimeout(poll, 1500);
    }
    function disconnect(error) {
      if (closed) return;
      source?.close();
      source = null;
      clearTimeout(timer);
      onDisconnect(error);
      if (!reconnected) {
        reconnected = true;
        timer = setTimeout(connect, 250);
      } else {
        timer = setTimeout(poll, 1500);
      }
    }
    function connect() {
      if (closed) return;
      if (!EventSource) { reconnected = true; disconnect(); return; }
      try {
        source = new EventSource(`${jobPath(jobId)}/events?after=${cursor}`);
      } catch (error) { disconnect(error); return; }
      const current = source;
      for (const name of ['pick', 'batch', 'facts', 'hub', 'note', 'picks_done', 'done', 'error']) {
        current.addEventListener(name, (event) => {
          if (closed || current !== source) return;
          if (typeof event.data !== 'string') { disconnect(); return; }
          let message;
          try { message = JSON.parse(event.data); } catch { disconnect(new ApiError('Invalid job event.')); return; }
          if (event.lastEventId && event.lastEventId !== `${jobId}:${message.seq}`) {
            disconnect(new ApiError('The event cursor belongs to another job.'));
            return;
          }
          deliver(name, message);
        });
      }
    }
    if (pollOnly || !EventSource) { reconnected = true; poll(); }
    else connect();
    return close;
  }

  function followLegacyJob(jobId, handlers = {}) {
    let source = null, closed = false, timer = null, polling = false;
    const controller = new AbortController();
    const close = () => {
      closed = true;
      source?.close();
      clearTimeout(timer);
      controller.abort();
    };
    function finish(name, value) {
      if (closed) return;
      close();
      handlers[name]?.(value);
    }
    async function poll(remaining = 600) {
      if (closed) return;
      try {
        const job = await request('GET', jobPath(jobId), undefined, {signal: controller.signal});
        if (closed) return;
        if (job.state === 'done') return finish('done', job.result || {});
        if (job.state === 'error') return finish('error', job.error || 'The job failed without a message');
      } catch (error) {
        if (closed) return;
        if (error.status === 404) return finish('error', 'The server no longer knows this job (it may have restarted).');
      }
      if (remaining <= 1) return finish('error', 'Gave up waiting for the job to finish.');
      timer = setTimeout(() => poll(remaining - 1), 1500);
    }
    function fallback() {
      if (polling || closed) return;
      polling = true; source?.close(); poll();
    }
    if (EventSource) {
      try {
        source = new EventSource(`${jobPath(jobId)}/events`);
        for (const name of ['progress', 'branch', 'pick', 'done', 'error']) {
          source.addEventListener(name, (event) => {
            if (closed || polling) return;
            if (typeof event.data !== 'string') { fallback(); return; }
            let data;
            try { data = JSON.parse(event.data); } catch { data = {}; }
            if (name === 'done') finish('done', data || {});
            else if (name === 'error') finish('error', data.error || 'The job failed without a message');
            else handlers[name]?.(data);
          });
        }
      } catch { fallback(); }
    } else fallback();
    return close;
  }
  return {request, followStepJob, followLegacyJob};
}

let browserApi;
const defaultApi = () => browserApi ??= createApi();
export const request = (...args) => defaultApi().request(...args);
export const followStepJob = (...args) => defaultApi().followStepJob(...args);
export const followLegacyJob = (...args) => defaultApi().followLegacyJob(...args);
