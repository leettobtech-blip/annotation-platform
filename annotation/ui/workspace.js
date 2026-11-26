const byId = (id) => document.getElementById(id);
const connect = byId('connect');
const picker = byId('picker');
const drop = byId('drop');
const question = byId('question');
const messages = byId('messages');
const files = new Map();
const pending = [];
const selected = new Set();

let formats;
let token;
let project;
let activeUploads = 0;
let previousTurn = null;
let asking = false;

function make(tag, content, className) {
  const item = document.createElement(tag);
  if (content !== undefined) item.textContent = content;
  if (className) item.className = className;
  return item;
}

async function request(path, { method = 'GET', json, body, contentType, idempotencyKey } = {}) {
  const headers = new Headers();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  if (json !== undefined) headers.set('Content-Type', 'application/json');
  if (contentType) headers.set('Content-Type', contentType);
  if (idempotencyKey) headers.set('Idempotency-Key', idempotencyKey);
  const response = await fetch(path, {
    method, headers, body: json === undefined ? body : JSON.stringify(json),
    redirect: 'error', signal: AbortSignal.timeout(120_000),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => null);
    const detail = typeof error?.detail === 'string' ? error.detail : null;
    throw new Error(detail || `Request failed (HTTP ${response.status})`);
  }
  return response.status === 204 ? null : response.json();
}

function projectPath(path = '') {
  return `/v1/projects/${project}${path}`;
}

async function waitForJob(jobId, onStatus = () => {}) {
  const deadline = Date.now() + 10 * 60 * 1000;
  let delay = 900;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, delay));
    const job = await request(projectPath(`/jobs/${jobId}`));
    if (job.status === 'succeeded') return job.result;
    if (job.status === 'failed') throw new Error(job.error || 'Processing failed');
    onStatus(job.status);
    delay = Math.min(delay * 1.35, 3500);
  }
  throw new Error('Processing is taking longer than expected.');
}

function refreshControls() {
  const ready = [...files.values()].some((file) => file.ready);
  const locked = asking || previousTurn !== null;
  for (const file of files.values()) file.checkbox.disabled = locked || !file.ready;
  byId('send').disabled = asking || !ready;
  byId('reset').disabled = asking || !previousTurn;
  byId('scope').textContent = selected.size
    ? `Searching ${selected.size} selected ${selected.size === 1 ? 'file' : 'files'}`
    : 'Searching all project files';
  if (previousTurn) byId('scope').textContent += ' · Start a new conversation to change sources';
}

function setFileState(row, state, text) {
  row.status.dataset.state = state;
  row.status.textContent = text;
  row.ready = state === 'ready';
  refreshControls();
}

function uploadType(file) {
  const suffix = file.name.toLowerCase().match(/\.[^.]+$/)?.[0];
  // WebM can be video or audio. Prefer the browser's declared supported type.
  if (formats.types[file.type]) return file.type;
  return Object.entries(formats.types).find(([, [, extension]]) => extension === suffix)?.[0];
}

function enqueueFiles(input) {
  byId('files-status').textContent = '';
  for (const file of input) {
    const id = crypto.randomUUID();
    const item = make('li', undefined, 'file');
    const checkbox = make('input');
    checkbox.id = `file-${id}`;
    checkbox.type = 'checkbox';
    checkbox.disabled = true;
    const label = make('label', file.name);
    label.htmlFor = checkbox.id;
    const status = make('span', undefined, 'file-state');
    status.id = `state-${id}`;
    checkbox.setAttribute('aria-describedby', status.id);
    label.append(status);
    item.append(checkbox, label);
    byId('files-list').append(item);
    const row = { name: file.name, checkbox, status, ready: false };
    files.set(id, row);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked && selected.size >= 20) {
        checkbox.checked = false;
        byId('files-status').textContent = 'Select up to 20 files.';
        return;
      }
      byId('files-status').textContent = '';
      if (checkbox.checked) selected.add(id);
      else selected.delete(id);
      refreshControls();
    });
    const contentType = uploadType(file);
    if (!contentType) setFileState(row, 'failed', 'Unsupported file format');
    else if (!file.size || file.size > formats.max_bytes) {
      setFileState(row, 'failed', `Choose a nonempty file under ${formats.max_bytes / 1024 ** 2} MiB`);
    } else {
      setFileState(row, 'queued', 'Waiting to upload');
      pending.push({ file, id, row, contentType });
    }
  }
  runUploads();
}

function runUploads() {
  while (activeUploads < 2 && pending.length) {
    activeUploads += 1;
    uploadFile(pending.shift()).finally(() => {
      activeUploads -= 1;
      runUploads();
    });
  }
}

async function uploadFile({ file, id, row, contentType }) {
  try {
    setFileState(row, 'uploading', 'Uploading');
    const { upload } = await request(projectPath(`/media/${id}`), {
      method: 'PUT', json: { content_type: contentType },
    });
    if (upload.fields) {
      const form = new FormData();
      for (const [name, value] of Object.entries(upload.fields)) form.append(name, value);
      form.append('file', file);
      const response = await fetch(upload.url, {
        method: 'POST', body: form, credentials: 'omit', signal: AbortSignal.timeout(120_000),
      });
      if (!response.ok) throw new Error(`Storage upload failed (HTTP ${response.status})`);
    } else {
      const url = new URL(upload.url, window.location.origin);
      if (url.origin !== window.location.origin) throw new Error('Invalid local upload destination');
      await request(url.href, { method: 'PUT', body: file, contentType });
    }
    setFileState(row, 'processing', 'Queued for processing');
    const job = await request(projectPath(`/assets/${id}/process`), {
      method: 'POST', json: {}, idempotencyKey: crypto.randomUUID(),
    });
    const result = await waitForJob(job.id, (status) => {
      setFileState(row, 'processing', status === 'running' ? 'Processing' : 'Queued for processing');
    });
    const indexed = [result.evidence, result.transcription, result.visual_evidence].includes('indexed');
    if (indexed) setFileState(row, 'ready', 'Ready to ask');
    else setFileState(row, 'unavailable', 'Processed · No searchable text');
  } catch (error) {
    setFileState(row, 'failed', error.message);
  }
}

connect.addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = connect.querySelector('button');
  button.disabled = true;
  token = byId('token').value.trim();
  byId('connect-status').textContent = '';
  try {
    if (!token) throw new Error('Enter an access token.');
    formats ||= await request('/upload-formats');
    project = crypto.randomUUID();
    await request(projectPath(), { method: 'PUT', json: { name: 'Browser workspace' } });
    picker.accept = [...new Set(Object.entries(formats.types).flatMap(([mime, [, suffix]]) => [mime, suffix]))].join(',');
    byId('token').value = '';
    connect.hidden = true;
    byId('panels').hidden = false;
    picker.focus();
  } catch (error) {
    token = undefined;
    byId('connect-status').textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

picker.addEventListener('change', () => {
  enqueueFiles(picker.files);
  picker.value = '';
});
drop.addEventListener('dragover', (event) => {
  event.preventDefault();
  drop.classList.add('dragging');
});
drop.addEventListener('dragleave', () => drop.classList.remove('dragging'));
drop.addEventListener('drop', (event) => {
  event.preventDefault();
  drop.classList.remove('dragging');
  enqueueFiles(event.dataTransfer.files);
});

function renderAnswer(answer, result) {
  const card = make('div', undefined, 'answer');
  card.append(make('h3', 'Answer'), make('p', result.answer, 'answer-body'));
  const citations = result.citations ?? [];
  if (citations.length) {
    const details = make('details', undefined, 'sources');
    details.open = true;
    details.append(make('summary', `Sources (${citations.length})`));
    const list = make('ol');
    for (const entry of citations) {
      const metadata = entry.metadata ?? {};
      const name = files.get(metadata.asset_id)?.name || metadata.title || 'Project source';
      const timing = Number.isFinite(metadata.start_ms) && Number.isFinite(metadata.end_ms)
        ? ` · ${(metadata.start_ms / 1000).toFixed(2)}–${(metadata.end_ms / 1000).toFixed(2)} s` : '';
      const item = make('li');
      item.append(make('strong', name + timing), make('p', entry.body, 'excerpt'));
      list.append(item);
    }
    details.append(list);
    card.append(details);
  }
  const structured = make('details', undefined, 'sources');
  structured.append(make('summary', 'Structured output (JSON)'), make('pre', JSON.stringify(result, null, 2)));
  card.append(structured);
  const turn = make('article', undefined, 'turn');
  turn.append(make('p', answer, 'question'), card);
  byId('chat-empty').hidden = true;
  messages.append(turn);
}

byId('ask').addEventListener('submit', async (event) => {
  event.preventDefault();
  const value = question.value.trim();
  if (!value || byId('send').disabled) return;
  asking = true;
  question.readOnly = true;
  refreshControls();
  byId('ask-status').textContent = '';
  byId('send').textContent = 'Finding evidence…';
  try {
    const json = { question: value };
    if (previousTurn) json.previous_turn = previousTurn;
    else if (selected.size) json.asset_ids = [...selected];
    const job = await request(projectPath('/chat'), {
      method: 'POST', json, idempotencyKey: crypto.randomUUID(),
    });
    renderAnswer(value, await waitForJob(job.id));
    previousTurn = job.id;
    question.value = '';
  } catch (error) {
    byId('ask-status').textContent = error.message;
  } finally {
    asking = false;
    question.readOnly = false;
    byId('send').textContent = 'Ask question';
    refreshControls();
  }
});

byId('reset').addEventListener('click', () => {
  previousTurn = null;
  messages.querySelectorAll('.turn').forEach((turn) => turn.remove());
  byId('chat-empty').hidden = false;
  byId('ask-status').textContent = '';
  refreshControls();
  question.focus();
});
