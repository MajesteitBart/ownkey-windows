/* Ownkey Meetings window. Plain JavaScript, no build step.
 * Talks to the local API served by the Ownkey backend. */
(() => {
  'use strict';

  const PARAMS = new URLSearchParams(location.search);
  const TOKEN = PARAMS.get('token') || '';
  const START_VIEW = PARAMS.get('view') || '';
  const START_TAB = ['thoughts', 'transcript', 'summary'].includes(PARAMS.get('tab')) ? PARAMS.get('tab') : '';
  const START_MEETING = PARAMS.get('meeting') || '';
  if (TOKEN) history.replaceState(null, '', location.pathname);
  // Inside Ownkey's own window the webview's Back / Reload / Save as menu has no
  // place. Text fields keep theirs for copy and paste. The flag survives a reload.
  if (PARAMS.get('shell') === 'app') sessionStorage.setItem('ownkey-shell', 'app');
  if (sessionStorage.getItem('ownkey-shell') === 'app') {
    document.documentElement.classList.add('shell-app');
    document.addEventListener('contextmenu', (e) => {
      if (!e.target.closest('input, textarea, [contenteditable="true"]')) e.preventDefault();
    });
  }

  const ICONS = {
    mic: '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2M12 19v3M8 22h8"/>',
    volume: '<path d="M11 5 6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 0 1 0 7M19 5a9 9 0 0 1 0 14"/>',
    play: '<path d="M7 4l13 8-13 8z"/>', pause: '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
    stop: '<rect x="5" y="5" width="14" height="14" rx="2.5"/>', plus: '<path d="M12 5v14M5 12h14"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>', check: '<path d="M20 6L9 17l-5-5"/>',
    x: '<path d="M18 6L6 18M6 6l12 12"/>', sliders: '<path d="M4 6h10M18 6h2M4 12h2M10 12h10M4 18h14M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="8" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>', copy: '<rect x="9" y="9" width="12" height="12" rx="2.5"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    download: '<path d="M12 3v12M6 9l6 6 6-6M4 21h16"/>', sparkle: '<path d="M12 2l2.4 7.2L22 12l-7.6 2.8L12 22l-2.4-7.2L2 12l7.6-2.8z"/>',
    edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>', text: '<path d="M4 6h16M4 12h10M4 18h7"/>',
    refresh: '<path d="M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6"/>', trash: '<path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"/>',
    alert: '<path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>', more: '<circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/>',
    arrowUp: '<path d="M12 19V5M5 12l7-7 7 7"/>', mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/>',
    cpu: '<rect x="5" y="5" width="14" height="14" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/>',
    lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>', users: '<circle cx="9" cy="8" r="3.5"/><path d="M2 20v-1a5 5 0 0 1 5-5h4a5 5 0 0 1 5 5v1"/><circle cx="17" cy="9" r="3"/><path d="M22 20v-1a4 4 0 0 0-3-3.9"/>',
    wave: '<path d="M4 14v-4M8 17V7M12 20V4M16 17V7M20 14v-4"/>', undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10h-3"/>',
  };
  const FILLED = new Set(['play', 'pause', 'stop', 'sparkle', 'more']);
  const ico = (name, size = 15, sw = 1.8) => `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="${FILLED.has(name) ? 'currentColor' : 'none'}" stroke="${FILLED.has(name) ? 'none' : 'currentColor'}" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ''}</svg>`;
  const MARK = '<svg viewBox="0 0 652 486" aria-hidden="true"><g fill="#F3F1EC"><path d="M60 98C25 111 0 151 0 193c0 55 39 97 90 102v-37c-31-6-51-31-51-64 0-24 8-43 21-55V98Z"/><path d="M592 98c35 13 60 53 60 95 0 55-39 97-90 102v-37c31-6 51-31 51-64 0-24-8-43-21-55V98Z"/><path d="M326 42C294 12 258-3 216 1 146 8 94 80 94 166c0 45 24 78 52 98 9 6 18 11 28 15-9 39-10 86 0 126 13 51 52 81 111 81h82c59 0 98-30 111-81 10-40 9-87 0-126 10-4 19-9 28-15 28-20 52-53 52-98C558 80 506 8 436 1c-42-4-78 11-110 41Z"/></g><g fill="#141414"><circle cx="221.5" cy="154.5" r="38"/><circle cx="431" cy="154.5" r="38.25"/></g><g fill="#DE5F14"><rect x="216" y="323" width="27" height="70" rx="13"/><rect x="265" y="287" width="27" height="139" rx="13"/><rect x="312" y="248" width="28" height="212" rx="13"/><rect x="360" y="288" width="27" height="136" rx="13"/><rect x="409" y="323" width="28" height="70" rx="13"/></g></svg>';
  const wave = (cls = '') => `<span class="wave ${cls}"><i></i><i></i><i></i><i></i><i></i></span>`;
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = (seconds) => { const s = Math.max(0, Math.floor(seconds || 0)); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60; return h ? `${h}:${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}` : `${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}`; };
  const fmtDate = (unix) => new Date((unix || 0) * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
  const fmtBytes = (n) => n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n >= 1e6 ? `${Math.round(n / 1e6)} MB` : `${Math.round(n / 1e3)} KB`;
  const meter = (level, total = 10) => { let s = ''; const on = Math.round((level || 0) * total); for (let i = 0; i < total; i++) s += `<i class="${i < on ? (i >= total - 1 ? 'hot' : 'on') : ''}"></i>`; return `<span class="meter">${s}</span>`; };
  const SOURCE = { mic: { label: 'Microphone', icon: 'mic' }, system: { label: 'Call audio', icon: 'volume' } };
  const ENGINE = { orukeet: 'Orukeet · local', openai: 'OpenAI · remote', mistral: 'Mistral · remote', google: 'Google · remote', custom: 'Custom endpoint' };

  // ── api ──────────────────────────────────────────────────────
  async function api(method, path, body) {
    const headers = { 'X-Ownkey-Token': TOKEN };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: 'same-origin' });
    let data = {};
    try { data = await response.json(); } catch (e) { data = {}; }
    if (response.status === 409 && data.consent) { const err = new Error(data.error || 'Confirmation needed'); err.consent = data.consent; throw err; }
    if (!response.ok) throw new Error(data.error || `${response.status} ${response.statusText}`);
    return data;
  }
  const get = (p) => api('GET', p), post = (p, b = {}) => api('POST', p, b), put = (p, b) => api('PUT', p, b), del = (p) => api('DELETE', p);

  // ── state ────────────────────────────────────────────────────
  const state = {
    status: null, meetings: [], view: 'empty', meetingId: null, detail: null, tab: 'thoughts',
    query: '', editing: null, showOriginal: new Set(), devices: null, playing: false, playTime: 0,
    form: { title: '', mic: true, system: true, mic_device: '', system_device: '', retention: 'days7', mic_shared: false, live_transcription: true, live_speakers: false },
    noteTimer: null, noteSaved: 'idle', menu: null,
  };

  // ── toasts and dialogs ───────────────────────────────────────
  function toast(message, kind = 'error', ms = 5000) {
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    el.innerHTML = `${ico(kind === 'error' ? 'alert' : 'check', 15, 2.2)}<span>${esc(message)}</span>`;
    $('#toasts').appendChild(el);
    setTimeout(() => el.remove(), ms);
  }
  function dialog(html) {
    const host = $('#dialogs');
    host.innerHTML = `<div class="modal-wrap"><div class="modal" role="dialog">${html}</div></div>`;
    return { close: () => { host.innerHTML = ''; }, root: host };
  }
  function confirmDialog({ title, body, confirm, danger = false }) {
    return new Promise((resolve) => {
      const d = dialog(`<div class="mh"><h2>${esc(title)}</h2></div><div class="mb">${body}</div>
        <div class="mf"><button class="btn quiet" data-no>Cancel</button><button class="btn ${danger ? 'danger' : 'primary'}" data-yes>${esc(confirm)}</button></div>`);
      $('[data-no]', d.root).onclick = () => { d.close(); resolve(false); };
      $('[data-yes]', d.root).onclick = () => { d.close(); resolve(true); };
    });
  }
  function consentDialog(consent) {
    const policyKey = consent.policy_key || 'remote';
    const upload = consent.kind === 'upload' || consent.kind === 'transcribe';
    const liveConsent = policyKey === 'live_speakers' || policyKey === 'live_transcription';
    return new Promise((resolve) => {
      const d = dialog(`<div class="mh"><span class="eyebrow">${upload ? 'Before the first audio upload' : 'Before the first remote analysis'}</span>
        <h2>${esc(consent.title || `Ownkey will send meeting text to ${consent.provider}`)}</h2>
        <p>${esc(consent.intro || 'Audio never leaves this PC. The transcript text does, over your own key. Check what goes out, then decide how to handle this next time.')}</p></div>
        <div class="mb"><dl class="kv">
          <dt>Provider</dt><dd>${esc(consent.provider)} · <span class="mono">${esc(consent.model)}</span><span class="ink-3">${esc(consent.host)}</span></dd>
          <dt>Sent</dt><dd>${(consent.sent || []).map((s) => `<span class="pill ${upload ? 'amber' : ''}">${esc(s)}</span>`).join('')}</dd>
          <dt>Not sent</dt><dd>${(consent.not_sent || []).map((s) => `<span class="pill green">${esc(s)}</span>`).join('')}</dd>
        </dl><div style="margin-top:14px"><label class="check"><input type="checkbox" data-remember>Don’t ask again for ${liveConsent ? (policyKey === 'live_speakers' ? 'live speaker labels' : 'live transcription') : esc(consent.provider)}</label></div>
        ${consent.retention ? `<p class="hint" style="margin-top:8px">${esc(consent.retention)}</p>` : ''}</div>
        <div class="mf"><button class="btn quiet" data-no>Cancel</button><button class="btn primary" data-yes>${ico('arrowUp', 14, 2.2)}${liveConsent ? (policyKey === 'live_speakers' ? 'Start live speaker labels' : 'Start live transcription') : consent.kind === 'transcribe' ? 'Upload and transcribe' : upload ? 'Upload and label speakers' : 'Send and continue'}</button></div>`);
      $('[data-no]', d.root).onclick = () => { d.close(); resolve(false); };
      $('[data-yes]', d.root).onclick = async () => {
        const remember = $('[data-remember]', d.root).checked;
        d.close();
        if (remember) { try { await post('/api/policy', { [policyKey]: 'allow' }); } catch (e) { toast(e.message); } }
        resolve(true);
      };
    });
  }
  /* run an analysis action; on 409 show the disclosure and retry once with remote_ok */
  async function withConsent(run) {
    try { return await run(false); } catch (e) {
      if (!e.consent) throw e;
      if (await consentDialog(e.consent)) return run(true);
      return null;
    }
  }

  // ── data loading ─────────────────────────────────────────────
  async function refreshStatus() { try { state.status = await get('/api/status'); } catch (e) { state.status = null; } }
  async function refreshList() { try { state.meetings = (await get('/api/meetings')).meetings; } catch (e) { toast(e.message); } }
  async function refreshDetail() {
    if (!state.meetingId) return;
    try { state.detail = await get(`/api/meetings/${state.meetingId}`); } catch (e) { state.detail = null; state.view = 'empty'; state.meetingId = null; toast(e.message); }
  }
  async function loadDevices() { if (!state.devices) { try { state.devices = await get('/api/devices'); } catch (e) { state.devices = { inputs: [], outputs: [], note: e.message }; } } }

  // ── rendering ────────────────────────────────────────────────
  function render() { state.liveStatusHTML = null; renderSidebar(); renderMain(); }

  function renderSidebar() {
    const status = state.status || {};
    const cap = status.capture;
    const items = state.meetings.map((m) => {
      const live = cap && cap.meeting_id === m.id && (cap.state === 'recording' || cap.state === 'paused');
      const dot = live ? 'rec' : m.state === 'interrupted' ? 'amber' : m.job ? 'accent' : m.summary_state === 'outdated' ? 'amber' : m.passages ? 'green' : '';
      const meta = live ? `REC ${fmt(cap.elapsed)}` : m.job ? (m.job.kind === 'transcribe' ? (m.incremental ? 'Finishing transcript' : `transcribing ${Math.round((m.job.progress || 0) * 100)}%`) : m.job.kind) : `${new Date(m.created_at * 1000).toLocaleDateString([], { day: 'numeric', month: 'short' })} · ${fmt(m.elapsed)}`;
      return `<button class="sb-item ${m.id === state.meetingId ? 'is-active' : ''}" data-open="${m.id}" title="${esc(m.title || 'Untitled meeting')}">
        <span class="t truncate">${esc(m.title || 'Untitled meeting')}</span><span class="m"><span class="dot ${dot}"></span><span class="truncate">${esc(meta)}${m.audio_state === 'removed' ? ' · audio removed' : ''}</span></span></button>`;
    }).join('');
    $('#sidebar').innerHTML = `
      <div class="sb-head"><span class="keycap">${MARK}</span><span class="name">Ownkey</span><small>Meetings</small></div>
      <button class="sb-new" data-new ${status.capturing ? 'disabled title="A meeting is already recording"' : ''}>${ico('plus', 15, 2.2)}New meeting</button>
      <div class="sb-section">Library</div>
      <div class="sb-list">${items || '<div class="sb-empty">No meetings yet. Start one, and it lands here with its notes, transcript and summary.</div>'}</div>
      <div class="sb-foot">
        <button data-settings>${ico('sliders', 14)}Ownkey settings</button>
        <div class="meta">${status.library ? `${fmtBytes(status.library.bytes)} on this PC` : ''}${status.local_model ? ` · Orukeet ${status.local_model.installed ? 'installed' : 'not installed'}` : ''}</div>
      </div>`;
    $$('[data-open]').forEach((b) => b.onclick = () => openMeeting(b.dataset.open));
    $('[data-new]').onclick = () => { state.view = 'new'; state.meetingId = null; state.detail = null; loadDevices().then(render); render(); };
    $('[data-settings]').onclick = () => post('/api/settings/open').catch((e) => toast(e.message));
  }

  function renderMain() {
    const main = $('#main');
    setTimeout(pollMicPreview, 0);  // starts the meter on the New meeting sheet, releases the microphone anywhere else
    if (state.view === 'new') { main.innerHTML = renderNew(); bindNew(); return; }
    if (state.view === 'meeting' && state.detail) { main.innerHTML = renderMeeting(); bindMeeting(); return; }
    const status = state.status || {};
    main.innerHTML = `<div class="doc-scroll"><div class="empty"><h1><span>Ownkey Meetings</span>Record, review, ask.</h1>
      <p>Record a meeting and see phrases appear as you speak. Use Orukeet on this PC or your selected transcription provider. Notes and transcripts are yours to keep, export or delete.</p>
      <div class="actions"><button class="btn accent md" data-start>${ico('mic', 14)}New meeting</button>${status.local_model && !status.local_model.installed ? `<button class="btn secondary md" data-settings2>${ico('cpu', 14)}Install Orukeet in Settings</button>` : ''}</div></div></div>`;
    $('[data-start]').onclick = () => { state.view = 'new'; loadDevices().then(render); render(); };
    const s2 = $('[data-settings2]'); if (s2) s2.onclick = () => post('/api/settings/open').catch((e) => toast(e.message));
  }

  // ── new meeting ──────────────────────────────────────────────
  function renderNew() {
    const status = state.status || {};
    const f = state.form, d = state.devices || { inputs: [], outputs: [], note: 'Loading devices…' };
    const model = status.local_model || {}, text = status.text_model || {}, engine = status.transcriber || {};
    const options = (list, selected, defaultLabel) => `<option value="">${esc(defaultLabel)}</option>` + list.map((x) => `<option value="${esc(x.id)}" ${String(x.id) === String(selected) ? 'selected' : ''}>${esc(x.name)}${x.default ? ' (default)' : ''}</option>`).join('');
    const retention = [['days7', 'Keep audio 7 days after transcription', 'Default. Text stays until you delete the meeting; citations keep working after the audio is gone.'], ['keep', 'Keep audio until I delete the meeting', ''], ['after_transcription', 'Remove audio as soon as transcription succeeds', 'Interrupted or failed jobs keep their audio until they finish.']];
    return `<div class="doc-scroll"><div class="sheet">
      <h1>New meeting</h1><p class="lead">Choose what Ownkey listens to. Nothing is written to disk until you press Start.</p>
      <section><span class="eyebrow">Title</span><div class="field">${ico('edit', 14)}<input type="text" data-title placeholder="Meeting title (optional)" value="${esc(f.title)}"></div></section>
      <section><span class="eyebrow">Sources</span>
        <div class="seg" style="grid-template-columns:repeat(3,1fr)">
          <button data-src="both" class="${f.mic && f.system ? 'is-on' : ''}">${ico('wave', 14)}Microphone + system audio</button>
          <button data-src="mic" class="${f.mic && !f.system ? 'is-on' : ''}">${ico('mic', 14)}Microphone</button>
          <button data-src="system" class="${!f.mic && f.system ? 'is-on' : ''}">${ico('volume', 14)}System audio</button>
        </div>
        <div class="srcgrid" style="margin-top:12px">
          <div class="card srcbox ${f.mic ? '' : 'off'}"><div class="h">${ico('mic', 15)}Microphone<span class="mic-live" data-mic-live title="Live input from the selected microphone. Nothing is kept until you press Start.">${f.mic ? meter((state.micLast || {}).level || 0, 8) : ''}</span></div>
            <div class="field sm">${ico('mic', 13)}<select data-mic-device ${f.mic ? '' : 'disabled'}>${options(d.inputs, f.mic_device, 'Default microphone')}</select></div>
            <p class="mic-note" data-mic-note hidden></p>
            <p>Stored as its own track, labelled Microphone. You can confirm it as “You” after the meeting.</p>
            <label class="check"><input type="checkbox" data-mic-shared ${f.mic_shared ? 'checked' : ''} ${f.mic ? '' : 'disabled'}>Several people share this microphone</label>
            <p class="hint">Speaker labels then cover this track too, so people in the room get their own names.</p></div>
          <div class="card srcbox ${f.system ? '' : 'off'}"><div class="h">${ico('volume', 15)}System audio</div>
            <div class="field sm">${ico('volume', 13)}<select data-system-device ${f.system ? '' : 'disabled'}>${options(d.outputs, f.system_device, 'Default output device')}</select></div>
            <p>Captures everything played through that output: the call, but also music and notifications. Stored as Call audio. Headphones keep it out of your microphone.</p></div>
        </div>
        ${d.note ? `<p class="hint" style="margin-top:8px">${esc(d.note)}</p>` : ''}
      </section>
      <section><span class="eyebrow">During the meeting</span><div class="card card-pad live-options">
        <label class="check"><input type="checkbox" data-live-transcription ${f.live_transcription ? 'checked' : ''}>Transcribe while recording</label>
        <p class="hint">Completed phrases appear after pauses. Long turns are processed in short windows.</p>
        <label class="check"><input type="checkbox" data-live-speakers ${f.live_speakers ? 'checked' : ''} ${f.live_transcription && (f.system || (f.mic && f.mic_shared)) && (status.speaker_labels || {}).configured ? '' : 'disabled'}>Show live speaker changes with pyannoteAI</label>
        <p class="hint">Uses call audio and shared microphones. Audio streams to pyannoteAI; Ownkey asks before the first use. ${!(status.speaker_labels || {}).configured ? 'Add a key in Settings to enable this.' : ''}</p>
      </div></section>
      <section><span class="eyebrow">Readiness</span><div class="card card-pad" style="padding-top:2px;padding-bottom:2px">
        <div class="row"><span class="l">${ico('cpu', 15)}Transcription · ${esc(engine.label || 'Orukeet')}${engine.model ? ` · ${esc(engine.model)}` : ' on this PC'}${engine.follows_dictation ? ' <span class="ink-3">(same as dictation)</span>' : ''}</span><span class="v">${engine.kind === 'cloud'
          ? (engine.configured ? `<span class="pill ${engine.remote ? 'amber' : 'green'}">${engine.remote ? 'Remote' : 'Local endpoint'}</span><span class="meta">${engine.remote ? (status.transcription_policy === 'allow' ? 'upload allowed' : 'asks before uploading') : (f.live_transcription ? 'updates while recording' : 'runs after Stop')}</span>` : '<span class="pill amber">Not configured</span><span class="meta">Settings › Meetings</span>')
          : (model.installed ? `<span class="pill green">Installed</span><span class="meta">${f.live_transcription ? 'updates while recording' : 'runs after Stop'}</span>` : '<span class="pill amber">Not installed</span><span class="meta">recording still works; transcription waits</span>')}</span></div>
        <div class="row"><span class="l">${ico('sparkle', 15)}Summary and questions · ${esc(text.label || text.provider || 'no provider')}${text.model ? ` · ${esc(text.model)}` : ''}</span><span class="v">${text.configured ? `<span class="pill ${text.remote ? 'amber' : 'green'}">${text.remote ? 'Remote' : 'Local'}</span>` : '<span class="pill">Not configured</span>'}<span class="meta">${text.remote ? (status.remote_policy === 'allow' ? 'allowed' : 'asks before first use') : ''}</span></span></div>
        <div class="row"><span class="l">${ico('users', 15)}Speaker labels · pyannoteAI ${esc(f.live_speakers ? 'Live-1' : (status.speaker_labels || {}).model || '')}</span><span class="v">${(status.speaker_labels || {}).configured ? '<span class="pill amber">Remote</span>' : '<span class="pill">No key</span>'}<span class="meta">${(status.speaker_labels || {}).configured ? ((f.live_speakers ? status.live_speakers_policy : status.upload_policy) === 'allow' ? 'upload allowed' : 'asks before uploading') : 'Settings › Meetings'}</span></span></div>
        <div class="row"><span class="l">${ico('lock', 15)}Library</span><span class="v"><span class="meta mono">${esc(status.library ? status.library.root : '')}</span></span></div>
      </div></section>
      <section><span class="eyebrow">Audio retention for this meeting</span><div class="card card-pad opts">
        ${retention.map(([key, label, sub]) => `<button class="opt ${f.retention === key ? 'is-on' : ''}" data-retention="${key}"><span class="box"><i></i></span><span class="txt">${esc(label)}${sub ? `<small>${esc(sub)}</small>` : ''}</span></button>`).join('')}
      </div></section>
      <div class="startbar"><span class="remind">${ico('alert', 16)}<span><b>Let everyone know you are recording.</b><br><span class="ink-3">Ownkey does not tell remote participants for you.</span></span></span><span class="grow"></span>
        <button class="btn quiet md" data-cancel>Cancel</button><button class="btn accent md" data-start ${status.capturing ? 'disabled' : ''}>${ico('mic', 14)}Start recording</button></div>
    </div></div>`;
  }
  function bindNew() {
    const f = state.form;
    $('[data-title]').oninput = (e) => { f.title = e.target.value; };
    $$('[data-src]').forEach((b) => b.onclick = () => { const v = b.dataset.src; f.mic = v !== 'system'; f.system = v !== 'mic'; render(); });
    $('[data-mic-device]').onchange = (e) => { f.mic_device = e.target.value; state.micQuietSince = 0; pollMicPreview(); };
    $('[data-mic-shared]').onchange = (e) => { f.mic_shared = e.target.checked; render(); };
    $('[data-live-transcription]').onchange = (e) => { f.live_transcription = e.target.checked; if (!f.live_transcription) f.live_speakers = false; render(); };
    $('[data-live-speakers]').onchange = (e) => { f.live_speakers = e.target.checked; render(); };
    $('[data-system-device]').onchange = (e) => { f.system_device = e.target.value; };
    $$('[data-retention]').forEach((b) => b.onclick = () => { f.retention = b.dataset.retention; render(); });
    $('[data-cancel]').onclick = () => { state.view = state.meetings.length ? 'empty' : 'empty'; render(); };
    $('[data-start]').onclick = async () => {
      const button = $('[data-start]'); button.disabled = true;
      try {
        const body = { title: f.title, mic: f.mic, system: f.system, retention: f.retention, mic_shared: f.mic && f.mic_shared };
        body.live_transcription = f.live_transcription;
        body.live_speakers = f.live_speakers && f.live_transcription && (f.system || (f.mic && f.mic_shared));
        if (f.mic_device !== '') body.mic_device = Number(f.mic_device);
        if (f.system_device !== '') body.system_device = f.system_device;
        let created;
        for (let attempt = 0; attempt < 3; attempt++) {
          try { created = await post('/api/meetings', body); break; }
          catch (e) {
            if (!e.consent || !['live_transcription', 'live_speakers'].includes(e.consent.policy_key)) throw e;
            if (!await consentDialog(e.consent)) { button.disabled = false; return; }
            body[`${e.consent.policy_key}_ok`] = true;
          }
        }
        if (!created) { button.disabled = false; return; }
        f.title = '';
        await Promise.all([refreshStatus(), refreshList()]);
        await openMeeting(created.meeting.id, 'thoughts');
      } catch (e) { toast(e.message); button.disabled = false; }
    };
  }

  // Live input meter in the Microphone card. The backend opens the selected
  // microphone for the meter only, keeps nothing, and closes it when these
  // polls stop: leaving the sheet, hiding the window, starting a recording.
  async function pollMicPreview() {
    clearTimeout(state.micTimer);
    const wanted = state.view === 'new' && state.form.mic && !(state.status || {}).capturing && document.visibilityState === 'visible';
    if (!wanted) {
      if (state.micPreviewOn) { state.micPreviewOn = false; del('/api/preview/mic').catch(() => {}); }
      return;
    }
    if (state.micBusy) return;
    state.micBusy = true;
    try {
      const device = state.form.mic_device;
      const r = await get(`/api/preview/mic?device=${encodeURIComponent(device)}`);
      state.micPreviewOn = true;
      if (device === state.form.mic_device) paintMicPreview(r);
    } catch (e) { paintMicPreview({ level: 0, error: e.message }); }
    state.micBusy = false;
    state.micTimer = setTimeout(pollMicPreview, 120);
  }
  function paintMicPreview(r) {
    const live = $('[data-mic-live]'), note = $('[data-mic-note]');
    state.micLast = r;  // a re-render of the sheet starts from the last reading, not from zero
    if (!live || !note) return;
    live.innerHTML = meter(r.level, 8);
    const now = Date.now();
    if (r.level > 0.04 || r.error) state.micQuietSince = 0; else if (!state.micQuietSince) state.micQuietSince = now;
    const quiet = state.micQuietSince && now - state.micQuietSince > 4000;
    const text = r.error ? `This microphone could not be opened: ${r.error}` : quiet ? 'No input from this microphone yet. Say something, or pick another one.' : '';
    note.hidden = !text; note.textContent = text; note.classList.toggle('bad', !!r.error);
  }
  document.addEventListener('visibilitychange', pollMicPreview);
  // Closing or reloading the window releases the microphone at once; the backend
  // would also close it by itself a few seconds after the last poll.
  window.addEventListener('pagehide', () => {
    if (state.micPreviewOn) fetch('/api/preview/mic', { method: 'DELETE', keepalive: true, headers: { 'X-Ownkey-Token': TOKEN } }).catch(() => {});
  });

  // ── meeting view ─────────────────────────────────────────────
  async function openMeeting(id, tab) {
    state.meetingId = id; state.view = 'meeting'; state.query = ''; state.editing = null; state.showOriginal = new Set();
    if (tab) state.tab = tab;
    stopAudio();
    await refreshDetail();
    if (state.detail && !tab) {
      const passages = state.detail.passages.length;
      state.tab = state.detail.capture ? 'thoughts' : state.detail.summary ? 'summary' : passages ? 'transcript' : state.tab;
    }
    render();
    loadAudio();
  }
  const live = () => { const c = state.detail && state.detail.capture; return c && (c.state === 'recording' || c.state === 'paused') ? c : null; };
  const runningJob = (kind) => (state.detail ? state.detail.jobs : []).find((j) => (j.state === 'running' || j.state === 'queued') && (!kind || j.kind === kind));
  const lastJob = (kind) => { const jobs = (state.detail ? state.detail.jobs : []).filter((j) => j.kind === kind); return jobs[jobs.length - 1]; };

  function renderMeeting() {
    const d = state.detail, m = d.meeting, cap = live();
    const names = Object.fromEntries(d.speakers.map((s) => [s.id, s.name]));
    const sources = (Object.keys(m.sources || {}).map((k) => SOURCE[k] ? SOURCE[k].label : k).join(' + ') || 'No sources')
      + (m.sources && m.sources.mic && m.sources.mic.shared ? ' (shared microphone)' : '');
    const tabs = [['thoughts', 'edit', 'My thoughts', d.notes.rev ? `<span class="badge">rev ${d.notes.rev}</span>` : ''],
      ['transcript', 'text', 'Transcript', runningJob('transcribe') ? `<span class="badge amber">${d.live ? 'Live' : `${Math.round((runningJob('transcribe').progress || 0) * 100)}%`}</span>` : d.passages.length ? `<span class="badge">${d.passages.length}</span>` : ''],
      ['summary', 'sparkle', 'Summary', d.summary ? `<span class="badge ${d.summary.outdated ? 'amber' : 'green'}">${d.summary.outdated ? 'outdated' : 'ready'}</span>` : '']];
    return `${cap ? renderRecBar(cap) : ''}${renderLiveStatus(d.live, cap)}
      <div class="mt-head"><h1 class="mt-title"><input data-rename value="${esc(m.title)}" placeholder="Untitled meeting" spellcheck="false"></h1>
        <div class="mt-meta"><span class="mono tnum">${fmt(cap ? cap.elapsed : m.elapsed)}</span><span class="sep"></span><span>${esc(fmtDate(m.created_at))}</span><span class="sep"></span><span>${esc(sources)}</span><span class="sep"></span>
          ${m.state === 'interrupted' ? '<span class="pill amber">Interrupted</span>' : ''}${m.audio_state === 'removed' ? '<span class="pill">Audio removed</span>' : `<span title="Retention: ${esc(m.retention)}">${ico('cpu', 13)} ${esc(ENGINE[m.engine] || (d.passages.length ? m.engine : ((state.status || {}).transcriber || {}).label || 'Orukeet'))}</span>`}</div>
        <div class="doc-tabs">${tabs.map(([key, icon, label, badge]) => `<button class="doc-tab ${state.tab === key ? 'is-active' : ''}" data-tab="${key}">${ico(icon, 14)}${label}${badge}</button>`).join('')}<span class="grow"></span>
          <span class="tools"><button class="icon-btn" data-export="md" title="Export as Markdown">${ico('download', 15)}</button><button class="icon-btn" data-more title="More">${ico('more', 15)}</button></span></div></div>
      <div class="doc-scroll" id="doc"><div class="doc">${renderBanners()}${state.tab === 'thoughts' ? renderNotes() : state.tab === 'transcript' ? renderTranscript(names) : renderSummary(names)}</div></div>
      ${state.tab === 'transcript' && d.passages.length && m.audio_state === 'kept' && !cap ? renderPlaybar(m) : ''}`;
  }
  function renderRecBar(cap) {
    const paused = cap.state === 'paused';
    return `<div class="rec-bar">${wave(paused ? 'flat amber' : 'live')}<span class="lbl ${paused ? 'paused' : ''}">${paused ? 'Paused' : 'Recording'}</span><span class="rec-time">${fmt(cap.elapsed)}</span>
      ${Object.keys(cap.levels || {}).map((k) => `<span class="src">${ico(SOURCE[k] ? SOURCE[k].icon : 'wave', 14)}<b>${SOURCE[k] ? SOURCE[k].label : k}</b>${meter(paused ? 0 : cap.levels[k], 8)}</span>`).join('')}
      ${cap.gaps ? `<span class="pill amber">${cap.gaps} gap${cap.gaps > 1 ? 's' : ''}</span>` : ''}<span class="grow"></span>
      ${paused ? `<button class="btn accent" data-resume>${ico('play', 13)}Resume</button>` : `<button class="btn secondary" data-pause>${ico('pause', 13)}Pause</button>`}<button class="btn primary" data-stop>${ico('stop', 13)}Stop</button></div>`;
  }
  function renderLiveStatus(info, cap) {
    if (!info) return '<div class="live-status" hidden></div>';
    const titles = { listening: 'Listening for a pause', transcribing: 'Transcribing recent speech', paused: 'Paused', finalizing: 'Finishing the transcript', done: 'Transcript complete', error: 'Transcription needs attention' };
    const active = cap && cap.state === 'paused' ? [] : (info.active_names || (info.active_speakers || []).map((id) => ((state.detail || {}).speakers || []).find((s) => s.id === id)?.name || 'Speaker'));
    const streams = Object.values(info.streams || {});
    const error = streams.find((s) => s.error);
    const label = cap && cap.state === 'paused' ? 'Paused' : info.pending_seconds > 30 && info.state !== 'error' ? `Catching up · ${Math.round(info.pending_seconds)}s remaining` : titles[info.state] || 'Listening';
    return `<div class="live-status" role="status"><span>${ico('text', 13)} ${esc(label)}</span>${active.length ? `<span class="speaking-now">${ico('users', 13)} ${esc(active.join(' + '))} speaking</span>` : ''}${streams.some((s) => s.state === 'connecting') ? '<span class="hint">Connecting speaker labels…</span>' : ''}${error ? `<span class="hint amber">${esc(error.error)}</span>` : ''}</div>`;
  }
  function renderBanners() {
    const d = state.detail, m = d.meeting, out = [];
    const tj = runningJob('transcribe'), sj = runningJob('summary'), dj = runningJob('draft');
    if (m.state === 'interrupted' && m.audio_state === 'kept' && !tj) out.push(`<div class="banner red">${ico('alert', 16)}<div class="body"><b>This recording was interrupted.</b><p>${esc((d.events.filter((e) => e.kind === 'interrupted').pop() || {}).detail || 'Ownkey stopped capturing.')} ${fmt(m.elapsed)} of audio and your notes are saved. Nothing resumed on its own.</p></div><div class="acts"><button class="btn primary xs" data-transcribe>${ico('play', 12)}Transcribe now</button></div></div>`);
    const engine = (state.status || {}).transcriber || {};
    if (tj && !d.live) out.push(`<div class="banner neutral">${ico('cpu', 16)}<div class="body"><b><span class="shimmer">${engine.kind === 'cloud' ? `Transcribing with ${esc(engine.label)}${engine.remote ? ' (remote)' : ''}` : 'Transcribing on this PC'}</span></b><p>${esc(tj.detail || 'Loading Orukeet')} · passages appear as each window finishes.</p><div class="progress"><i style="width:${Math.round((tj.progress || 0) * 100)}%"></i></div></div></div>`);
    const failed = ['transcribe', 'speakers', 'summary', 'draft'].map(lastJob).filter((j) => j && j.state === 'error'
      && (j.kind === 'transcribe' || (j.kind === 'speakers' && state.tab === 'transcript') || ((j.kind === 'summary' || j.kind === 'draft') && state.tab === 'summary')));
    const names = { transcribe: 'Transcription', speakers: 'Speaker labelling', summary: 'Summary', draft: 'Draft' };
    for (const job of failed) out.push(`<div class="banner red">${ico('alert', 16)}<div class="body"><b>${names[job.kind]} failed.</b><p>${esc(job.error)}</p></div><div class="acts">${job.kind === 'transcribe' ? `<button class="btn secondary xs" data-transcribe>${ico('refresh', 12)}Retry</button>` : job.kind === 'speakers' ? `<button class="btn secondary xs" data-speakers>${ico('refresh', 12)}Retry</button>` : ''}</div></div>`);
    const pj = runningJob('speakers');
    if (pj && state.tab === 'transcript') out.push(`<div class="banner neutral">${ico('users', 16)}<div class="body"><b><span class="shimmer">Labelling speakers with pyannoteAI</span></b><p>${esc(pj.detail || '')} · the track was uploaded for this step only.</p></div></div>`);
    if (sj || dj) out.push(`<div class="banner neutral">${ico('sparkle', 16)}<div class="body"><b><span class="shimmer">${sj ? 'Generating the summary' : 'Drafting the follow-up'}</span></b><p>${esc((sj || dj).detail || '')}</p></div></div>`);
    if (!live() && m.state === 'stopped' && !d.passages.length && !tj && !failed.length && m.audio_state === 'kept') out.push(`<div class="banner neutral">${ico('info', 16)}<div class="body"><b>Not transcribed yet.</b><p>${engine.kind === 'cloud' ? `Transcribing sends the audio to ${esc(engine.label)}${engine.remote ? '; Ownkey asks first' : ' on your local endpoint'}.` : 'Orukeet transcribes on this PC; nothing is uploaded.'}</p></div><div class="acts"><button class="btn secondary xs" data-transcribe>${ico('play', 12)}Transcribe</button></div></div>`);
    return out.join('');
  }
  function renderNotes() {
    const d = state.detail;
    const saved = state.noteSaved === 'saving' ? 'Saving…' : state.noteSaved === 'dirty' ? 'Unsaved' : `Saved · rev ${d.notes.rev}`;
    return `<div class="notes-bar"><button class="btn secondary xs" data-stamp>${ico('clock', 12)}Insert timestamp</button><span class="ink-3 truncate">Yours alone. AI never writes here, and notes stay out of AI context unless you include them.</span><span class="grow"></span><span class="autosave"><span class="dot ${state.noteSaved === 'idle' ? 'green' : 'amber'}"></span>${esc(saved.toUpperCase())}</span></div>
      <textarea class="notes" data-notes placeholder="Type while the meeting runs. Add [12:04] timestamps to jump to that moment later." spellcheck="false">${esc(d.notes.content)}</textarea>`;
  }
  function renderTranscript(names) {
    const d = state.detail, q = state.query.trim().toLowerCase();
    const speakers = d.speakers.map((s) => `<span class="speaker ${s.source} ${s.confirmed ? '' : 'unconfirmed'}" data-speaker="${s.id}" title="${s.confirmed ? 'Confirmed' : 'Click to rename or confirm'}"><span class="mg">${esc(s.name.slice(0, 1).toUpperCase())}</span>${esc(s.name)}${s.confirmed ? '' : '<span class="q">?</span>'}</span>`).join('');
    const events = q ? [] : d.events.filter((e) => e.kind === 'gap' || e.kind === 'pause');
    const sysLine = (e) => `<div class="sys ${e.kind === 'gap' ? 'warn' : ''}">${ico(e.kind === 'gap' ? 'alert' : 'info', 12)}${fmt(e.at)} · ${esc(e.kind === 'gap' ? e.detail : 'paused here')}<span class="line"></span></div>`;
    const rows = [];
    let shown = 0, eventIndex = 0, previous = null;
    for (const p of d.passages) {
      while (eventIndex < events.length && events[eventIndex].at <= p.start) { rows.push(sysLine(events[eventIndex++])); previous = null; }
      const text = p.corrected || p.text;
      if (q && !text.toLowerCase().includes(q)) continue;
      shown++;
      const s = d.speakers.find((x) => x.id === p.speaker_id) || { source: p.source, name: p.speaker_id, confirmed: 1 };
      const editing = state.editing === p.id;
      const original = state.showOriginal.has(p.id);
      const inline = `<span data-text="${p.id}" data-passage="${p.id}" title="${fmt(p.start)} · Click to correct">${highlight(original ? p.text : text, q)}</span>`;
      const continues = previous && !q && !editing && !state.editing && !p.corrected && !previous.corrected
        && p.speaker_id === previous.speaker_id && p.source === previous.source && p.start - previous.end < 2
        && !/[.!?…]["'”’)]*$/.test(previous.text.trim());
      if (continues) {
        rows[rows.length - 1] = rows[rows.length - 1].replace('<!--continue-->', ` ${inline}<!--continue-->`);
        previous = p;
        continue;
      }
      const body = editing ? `<textarea class="edit" data-edit="${p.id}">${esc(text)}</textarea><div class="edit-acts"><button class="btn primary xs" data-save-edit="${p.id}">Save</button><button class="btn quiet xs" data-cancel-edit>Cancel</button>${p.corrected ? `<button class="btn quiet xs" data-revert="${p.id}">Back to recognition</button>` : ''}</div>`
        : `<div class="text">${inline}<!--continue--></div>`;
      rows.push(`<div class="passage" data-passage="${p.id}"><button class="ts" data-seek="${p.start}" title="Play from ${fmt(p.start)}">${fmt(p.start)}</button><div>
        <div class="who"><button class="speaker ${s.source} ${s.confirmed ? '' : 'unconfirmed'}" data-assign="${p.id}" title="Change who said this"><span class="mg">${esc(s.name.slice(0, 1).toUpperCase())}</span>${esc(s.name)}</button>
        ${p.corrected ? `<span class="rev">${ico('edit', 11)} edited · <button data-toggle-original="${p.id}">${original ? 'show correction' : 'show original'}</button></span>` : ''}${p.quality === 'window' ? '<span class="rev" title="No token timing for this passage">≈ time</span>' : ''}</div>${body}</div></div>`);
      previous = p;
    }
    while (eventIndex < events.length) rows.push(sysLine(events[eventIndex++]));
    const empty = d.passages.length ? (shown ? '' : `<p class="hint">No passage matches “${esc(state.query)}”.</p>`) : (runningJob('transcribe') ? '' : '<p class="hint">Nothing to show yet.</p>');
    const labels = (state.status && state.status.speaker_labels) || {};
    const labelled = d.speakers.some((s) => s.id.includes('-'));
    const canLabel = d.passages.length && d.meeting.audio_state === 'kept' && !runningJob() && !live();
    const labelButton = !d.passages.length ? '' : labels.configured
      ? `<button class="btn secondary xs" data-speakers ${canLabel ? '' : 'disabled'} title="Uploads the call audio track to pyannoteAI; asks first">${ico('users', 12)}${labelled ? 'Redo speaker labels' : 'Add speaker labels'}</button>`
      : `<button class="btn quiet xs" data-settings-meetings title="Speaker labels need a pyannoteAI key">${ico('users', 12)}Speaker labels: add a key in Settings</button>`;
    return `<div class="tx-tools"><div class="field sm">${ico('search', 13)}<input data-search placeholder="Search this transcript" value="${esc(state.query)}">${q ? `<span class="mono ink-3" style="font-size:11px">${shown} of ${d.passages.length}</span>` : ''}</div>${labelButton}<span class="grow"></span><span class="hint">${d.passages.length} passages · rev ${d.meeting.transcript_rev}</span></div>
      <div class="speakers">${speakers}${d.speakers.length ? '<span class="hint" style="align-self:center">Click a name to rename or confirm it.</span>' : ''}</div>${rows.join('')}${empty}`;
  }
  function highlight(text, q) { const safe = esc(text); if (!q) return safe; const re = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'gi'); return safe.replace(re, (m) => `<mark>${m}</mark>`); }
  function renderPlaybar(m) {
    return `<div class="playbar"><button class="icon-btn" data-play title="Play or pause">${ico(state.playing ? 'pause' : 'play', 14)}</button><span class="t" data-play-time>${fmt(state.playTime)}</span><div class="track" data-track><i data-track-fill style="width:${m.elapsed ? Math.min(100, state.playTime / m.elapsed * 100) : 0}%"></i></div><span class="t">${fmt(m.elapsed)}</span>
      <span class="keep">${m.retention === 'keep' ? 'Audio kept until you delete the meeting' : m.retention === 'days7' ? 'Audio kept 7 days after transcription' : 'Audio removed after transcription'}</span><button class="btn quiet xs" data-remove-audio>${ico('trash', 12)}Remove audio now</button></div>`;
  }
  function renderSummary(names) {
    const d = state.detail, s = d.summary, m = d.meeting, status = state.status || {}, text = status.text_model || {};
    const times = Object.fromEntries(d.passages.map((p) => [p.id, fmt(p.start)]));
    const cites = (refs) => (refs || []).filter((r) => times[r]).map((r) => `<button class="cite" data-cite="${r}"><span class="dot"></span>${times[r]}</button>`).join('');
    const item = (mk, inner, html, meta) => `<div class="item"><span class="mk ${mk}">${inner}</span><div>${html}${meta ? `<div class="meta">${meta}</div>` : ''}</div></div>`;
    let body = '';
    if (!d.passages.length) body = `<p class="hint">Transcribe the meeting first. Summary, questions and drafts are built on passages with timestamps.</p>`;
    else if (!s) body = `<div class="banner neutral">${ico('sparkle', 16)}<div class="body"><b>No summary yet.</b><p>Each of these is an explicit action. ${text.remote ? `Text goes to ${esc(text.label || text.provider)} over your own key; audio never does.` : 'The text model runs locally.'}</p></div></div>`;
    else {
      const c = s.content;
      body = `${s.outdated ? `<div class="banner">${ico('alert', 16)}<div class="body"><b>Summary is based on transcript rev ${s.input_rev}.</b><p>You changed the transcript since (now rev ${m.transcript_rev}). Citations still point at the same passages; wording may not.</p></div><div class="acts"><button class="btn secondary xs" data-summary>${ico('refresh', 12)}Regenerate</button></div></div>` : ''}
        <div class="sum"><h3>Overview</h3><p class="lead">${esc(c.overview || '')}</p>
        ${c.decisions && c.decisions.length ? `<h3>Decisions <span class="cnt">${c.decisions.length}</span></h3>${c.decisions.map((x) => item(x.status === 'decided' ? 'green' : 'amber', x.status === 'decided' ? ico('check', 11, 3.5) : '!', esc(x.text), `<span class="tagd ${x.status}">${x.status}</span>${x.unverified ? '<span class="tagd unverified" title="No cited passage shares words with this claim">unverified</span>' : ''}${cites(x.refs)}`)).join('')}` : ''}
        ${c.actions && c.actions.length ? `<h3>Action items <span class="cnt">${c.actions.length}${c.actions.some((a) => !a.owner) ? ' · some without owner' : ''}</span></h3>${c.actions.map((x) => item('neutral', '→', esc(x.text), `${x.owner ? `<span>${esc(x.owner)}</span>` : '<span class="unassigned">Unassigned</span>'}<span>·</span>${x.due ? `<span>${esc(x.due)}</span>` : '<span class="unassigned">No date</span>'}${x.unverified ? '<span class="tagd unverified">unverified</span>' : ''}${cites(x.refs)}`)).join('')}` : ''}
        ${c.questions && c.questions.length ? `<h3>Open questions <span class="cnt">${c.questions.length}</span></h3>${c.questions.map((x) => item('q', '?', esc(x.text), `${x.unverified ? '<span class="tagd unverified">unverified</span>' : ''}${cites(x.refs)}`)).join('')}` : ''}
        </div><div class="disclosure"><span class="pill ${text.remote ? 'amber' : 'green'}">${text.remote ? 'Remote' : 'Local'}</span>${esc(fmtDate(s.created_at))} · ${esc(s.model || s.provider)} · transcript rev ${s.input_rev} · notes ${s.include_notes ? 'included' : 'excluded'}</div>`;
    }
    const answers = d.answers.map((a) => `<div class="qa"><div class="q">${esc(a.question)}</div><div class="a">${esc(a.content)}${a.state === 'unanswerable' ? ' <span class="pill amber">No supporting passage</span>' : ''} ${cites(a.refs)}</div>${a.include_notes ? '<div class="hint">Your notes were included for this question.</div>' : ''}</div>`).join('');
    const draft = d.drafts.length ? d.drafts[d.drafts.length - 1] : null;
    return `${body}
      ${d.passages.length ? `<div class="actions">${s ? '' : `<button class="btn secondary" data-summary>${ico('sparkle', 14)}Generate summary</button>`}<button class="btn secondary" data-draft>${ico('mail', 14)}${draft ? 'Redo follow-up draft' : 'Draft follow-up'}</button><label class="check" style="margin-left:6px"><input type="checkbox" data-include-notes>Include My thoughts</label></div>
      <div class="ask"><span class="eyebrow">Ask this meeting</span><div class="composer" style="margin-top:10px"><textarea data-question rows="2" placeholder="What did we decide about…"></textarea><div class="ctl"><span class="hint">Answers cite passages that exist in this revision, and say so when the record cannot answer.</span><button class="send" data-ask title="Ask">${ico('arrowUp', 15, 2.4)}</button></div></div>${answers}</div>
      ${draft ? `<div class="ask"><span class="eyebrow">Follow-up draft</span><textarea class="draft" data-draft-text spellcheck="false" style="margin-top:10px">${esc(draft.content)}</textarea><div class="actions" style="margin-top:10px"><button class="btn primary" data-copy-draft>${ico('copy', 14)}Copy</button><span class="hint">No send action. Paste it where you want it; timestamps in brackets point at passages.</span></div></div>` : ''}` : ''}`;
  }

  // ── meeting bindings ─────────────────────────────────────────
  function bindMeeting() {
    const id = state.meetingId;
    const rename = $('[data-rename]');
    rename.onchange = () => put(`/api/meetings/${id}`, { title: rename.value }).then(refreshList).then(renderSidebar).catch((e) => toast(e.message));
    $$('[data-tab]').forEach((b) => b.onclick = () => { state.tab = b.dataset.tab; render(); });
    const on = (sel, fn) => $$(sel).forEach((b) => b.onclick = fn);
    on('[data-pause]', () => post(`/api/meetings/${id}/pause`).then(tick).catch((e) => toast(e.message)));
    on('[data-resume]', () => post(`/api/meetings/${id}/resume`).then(tick).catch((e) => toast(e.message)));
    const transcribe = () => withConsent((ok) => post(`/api/meetings/${id}/transcribe`, { remote_ok: ok })).then((r) => r && tick()).catch((e) => toast(e.message));
    on('[data-stop]', () => post(`/api/meetings/${id}/stop`).then(async () => {
      toast('Recording stopped.', 'ok', 2500);
      await tick();
      // a cloud engine that still needs the user's go-ahead has no job yet: ask now
      if (state.meetingId === id && !runningJob('transcribe') && !(state.detail && state.detail.passages.length)) transcribe();
    }).catch((e) => toast(e.message)));
    on('[data-transcribe]', transcribe);
    on('[data-speakers]', (e) => {
      const m = state.detail.meeting;
      const has = (k) => !!(m.sources && m.sources[k]);
      const run = (tracks) => withConsent((ok) => post(`/api/meetings/${id}/speakers`, { remote_ok: ok, tracks })).then((r) => r && tick()).catch((er) => toast(er.message));
      if (!(has('mic') && has('system'))) return run(has('system') ? ['system'] : ['mic']);
      const shared = !!(m.sources.mic && m.sources.mic.shared);
      openMenu(e.currentTarget, [
        { label: 'Which tracks have several people?', header: true },
        { label: `Call audio only${shared ? '' : ' (current)'}`, icon: 'volume', run: () => run(['system']) },
        { label: `Call audio + microphone${shared ? ' (current)' : ''}`, icon: 'users', run: () => put(`/api/meetings/${id}`, { mic_shared: true }).then(() => run(['mic', 'system'])).catch((er) => toast(er.message)) },
        { label: 'Microphone only', icon: 'mic', run: () => run(['mic']) },
      ]);
    });
    on('[data-settings-meetings]', () => post('/api/settings/open').catch((e) => toast(e.message)));
    on('[data-export]', (e) => { window.location.href = `/api/meetings/${id}/export?format=${e.currentTarget.dataset.export}&token=${encodeURIComponent(TOKEN)}`; });
    on('[data-more]', (e) => openMenu(e.currentTarget, [
      { label: 'Export as Markdown', icon: 'download', run: () => { location.href = `/api/meetings/${id}/export?format=md&token=${encodeURIComponent(TOKEN)}`; } },
      { label: 'Export as JSON', icon: 'download', run: () => { location.href = `/api/meetings/${id}/export?format=json&token=${encodeURIComponent(TOKEN)}`; } },
      { sep: true },
      { label: 'Delete meeting', icon: 'trash', danger: true, run: deleteMeeting },
    ]));
    const notes = $('[data-notes]');
    if (notes) {
      notes.oninput = () => { state.noteSaved = 'dirty'; clearTimeout(state.noteTimer); state.noteTimer = setTimeout(saveNotes, 800); };
      notes.onblur = () => { if (state.noteSaved === 'dirty') saveNotes(); };
      $('[data-stamp]').onclick = () => { const cap = live(); const t = cap ? cap.elapsed : state.playTime; const at = notes.selectionStart; const stamp = `[${fmt(t)}] `; notes.setRangeText(stamp, at, at, 'end'); notes.focus(); notes.oninput(); };
    }
    const search = $('[data-search]');
    if (search) { search.oninput = () => { state.query = search.value; const at = search.selectionStart; render(); const again = $('[data-search]'); again.focus(); again.setSelectionRange(at, at); }; }
    on('[data-seek]', (e) => seek(Number(e.currentTarget.dataset.seek)));
    on('[data-text]', (e) => { if ((live() || runningJob('transcribe')) && !state.detail.live) return; state.editing = e.currentTarget.dataset.text; render(); const ta = $('[data-edit]'); if (ta) { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); } });
    on('[data-cancel-edit]', () => { state.editing = null; render(); });
    on('[data-save-edit]', (e) => { const pid = e.currentTarget.dataset.saveEdit; put(`/api/meetings/${id}/passages/${pid}`, { corrected: $('[data-edit]').value }).then(() => { state.editing = null; return refreshDetail(); }).then(render).catch((er) => toast(er.message)); });
    on('[data-revert]', (e) => put(`/api/meetings/${id}/passages/${e.currentTarget.dataset.revert}`, { corrected: null }).then(() => { state.editing = null; return refreshDetail(); }).then(render).catch((er) => toast(er.message)));
    on('[data-toggle-original]', (e) => { const pid = e.currentTarget.dataset.toggleOriginal; state.showOriginal.has(pid) ? state.showOriginal.delete(pid) : state.showOriginal.add(pid); render(); });
    on('[data-assign]', (e) => { const pid = e.currentTarget.dataset.assign; openMenu(e.currentTarget, [{ label: 'Who said this?', header: true }, ...state.detail.speakers.map((s) => ({ label: s.name, icon: SOURCE[s.source] ? SOURCE[s.source].icon : 'users', run: () => put(`/api/meetings/${id}/passages/${pid}`, { speaker_id: s.id }).then(refreshDetail).then(render).catch((er) => toast(er.message)) }))]); });
    on('[data-speaker]', (e) => speakerMenu(e.currentTarget, e.currentTarget.dataset.speaker));
    on('[data-play]', togglePlay);
    on('[data-track]', (e) => { const r = e.currentTarget.getBoundingClientRect(); seek((e.clientX - r.left) / r.width * (state.detail.meeting.elapsed || 0)); });
    on('[data-remove-audio]', async () => { if (await confirmDialog({ title: 'Remove this meeting’s audio?', body: '<p class="hint">Playback and re-transcription stop working. The transcript, notes, summary and citations stay.</p>', confirm: 'Remove audio', danger: true })) post(`/api/meetings/${id}/remove-audio`).then(() => { stopAudio(); return tick(); }).catch((er) => toast(er.message)); });
    on('[data-summary]', () => withConsent((ok) => post(`/api/meetings/${id}/summary`, { include_notes: includeNotes(), remote_ok: ok })).then((r) => r && tick()).catch((e) => toast(e.message)));
    on('[data-draft]', () => withConsent((ok) => post(`/api/meetings/${id}/draft`, { remote_ok: ok })).then((r) => r && tick()).catch((e) => toast(e.message)));
    on('[data-ask]', ask);
    const q = $('[data-question]');
    if (q) q.onkeydown = (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(); } };
    on('[data-cite]', (e) => { const pid = e.currentTarget.dataset.cite; state.tab = 'transcript'; state.query = ''; render(); const el = $(`[data-passage="${pid}"]`); if (el) { el.classList.add('is-cited'); el.scrollIntoView({ block: 'center', behavior: 'smooth' }); } });
    on('[data-copy-draft]', () => navigator.clipboard.writeText($('[data-draft-text]').value).then(() => toast('Draft copied.', 'ok', 2000)).catch(() => toast('Could not copy; select the text and copy it yourself.')));
  }
  const includeNotes = () => { const c = $('[data-include-notes]'); return !!(c && c.checked); };
  async function saveNotes() {
    const notes = $('[data-notes]'); if (!notes || !state.meetingId) return;
    state.noteSaved = 'saving';
    try { const r = await put(`/api/meetings/${state.meetingId}/notes`, { content: notes.value }); if (state.detail) state.detail.notes = r.notes; state.noteSaved = 'idle'; }
    catch (e) { state.noteSaved = 'dirty'; toast(e.message); }
    const bar = $('.notes-bar .autosave'); if (bar) bar.innerHTML = `<span class="dot ${state.noteSaved === 'idle' ? 'green' : 'amber'}"></span>${state.noteSaved === 'idle' ? `SAVED · REV ${state.detail.notes.rev}` : 'UNSAVED'}`;
  }
  async function ask() {
    const q = $('[data-question]'); const question = q ? q.value.trim() : '';
    if (!question) return;
    const send = $('[data-ask]'); send.disabled = true; send.innerHTML = `<span class="shimmer">…</span>`;
    try { const r = await withConsent((ok) => post(`/api/meetings/${state.meetingId}/ask`, { question, include_notes: includeNotes(), remote_ok: ok })); if (r) { q.value = ''; await refreshDetail(); render(); } else { send.disabled = false; send.innerHTML = ico('arrowUp', 15, 2.4); } }
    catch (e) { toast(e.message); send.disabled = false; send.innerHTML = ico('arrowUp', 15, 2.4); }
  }
  async function deleteMeeting() {
    const m = state.detail.meeting;
    const ok = await confirmDialog({ title: `Delete “${m.title || 'Untitled meeting'}”?`, confirm: 'Delete meeting', danger: true,
      body: `<div class="row"><span class="l">${ico('wave', 15)}Audio, transcript (${state.detail.passages.length} passages), notes, summary, answers, drafts</span></div><div class="row"><span class="l">${ico('info', 15)}Running jobs are cancelled. Copies you exported stay where you put them. This is not a forensic wipe of the disk or backups.</span></div>` });
    if (!ok) return;
    try { await del(`/api/meetings/${m.id}`); stopAudio(); state.meetingId = null; state.detail = null; state.view = 'empty'; await Promise.all([refreshStatus(), refreshList()]); render(); toast('Meeting deleted.', 'ok', 2500); } catch (e) { toast(e.message); }
  }
  function speakerMenu(anchor, sid) {
    const s = state.detail.speakers.find((x) => x.id === sid); if (!s) return;
    const id = state.meetingId;
    const save = (name, confirmed) => put(`/api/meetings/${id}/speakers/${sid}`, { name, confirmed }).then(refreshDetail).then(render).catch((e) => toast(e.message));
    openMenu(anchor, [{ label: `${SOURCE[s.source] ? SOURCE[s.source].label : s.source} track · ${state.detail.passages.filter((p) => p.speaker_id === sid).length} passages`, header: true },
      { input: s.name, placeholder: 'Name', run: (value) => save(value, true) },
      ...(s.source === 'mic' && s.name !== 'You' ? [{ label: 'This microphone is me: call it “You”', icon: 'check', run: () => save('You', true) }] : []),
      ...(s.confirmed ? [] : [{ label: 'Confirm this name', icon: 'check', run: () => save(s.name, true) }]),
    ]);
  }
  function openMenu(anchor, items) {
    closeMenu();
    const r = anchor.getBoundingClientRect();
    const menu = document.createElement('div');
    menu.className = 'menu';
    menu.style.left = `${Math.min(r.left, window.innerWidth - 260)}px`; menu.style.top = `${r.bottom + 6}px`;
    menu.innerHTML = items.map((it, i) => it.sep ? '<div class="sepl"></div>' : it.header ? `<div class="lbl">${esc(it.label)}</div>` : it.input !== undefined ? `<div class="field sm">${ico('edit', 13)}<input data-menu-input="${i}" value="${esc(it.input)}" placeholder="${esc(it.placeholder || '')}"></div>` : `<button class="mi" data-menu-item="${i}" ${it.danger ? 'style="color:var(--red)"' : ''}>${it.icon ? ico(it.icon, 15) : ''}${esc(it.label)}</button>`).join('');
    document.body.appendChild(menu); state.menu = menu;
    $$('[data-menu-item]', menu).forEach((b) => b.onclick = () => { closeMenu(); items[Number(b.dataset.menuItem)].run(); });
    $$('[data-menu-input]', menu).forEach((inp) => { inp.focus(); inp.select(); inp.onkeydown = (e) => { if (e.key === 'Enter') { const v = inp.value.trim(); closeMenu(); if (v) items[Number(inp.dataset.menuInput)].run(v); } if (e.key === 'Escape') closeMenu(); }; });
    setTimeout(() => document.addEventListener('pointerdown', onOutside, { once: true }), 0);
  }
  function onOutside(e) { if (state.menu && !state.menu.contains(e.target)) closeMenu(); else if (state.menu) setTimeout(() => document.addEventListener('pointerdown', onOutside, { once: true }), 0); }
  function closeMenu() { if (state.menu) { state.menu.remove(); state.menu = null; } }

  // ── audio playback (both tracks together) ────────────────────
  const players = () => ['mic', 'system'].map((k) => $(`#audio-${k}`));
  function loadAudio() {
    const d = state.detail; stopAudio();
    if (!d || d.meeting.audio_state !== 'kept' || live()) return;
    for (const key of Object.keys(d.meeting.sources || {})) { const el = $(`#audio-${key}`); if (el) { el.src = `/api/meetings/${d.meeting.id}/audio/${key}.wav`; } }
  }
  function stopAudio() { players().forEach((p) => { p.pause(); p.removeAttribute('src'); p.load(); }); state.playing = false; state.playTime = 0; }
  function seek(t) { players().forEach((p) => { if (p.getAttribute('src')) { p.currentTime = t; p.play().catch(() => {}); } }); state.playing = true; state.playTime = t; state.tab === 'transcript' && updatePlaybar(); }
  function togglePlay() { const active = players().filter((p) => p.getAttribute('src')); if (!active.length) return; if (state.playing) { active.forEach((p) => p.pause()); state.playing = false; } else { active.forEach((p) => p.play().catch(() => {})); state.playing = true; } updatePlaybar(); }
  function updatePlaybar() { const btn = $('[data-play]'); if (btn) btn.innerHTML = ico(state.playing ? 'pause' : 'play', 14); const t = $('[data-play-time]'); if (t) t.textContent = fmt(state.playTime); const fill = $('[data-track-fill]'); const m = state.detail && state.detail.meeting; if (fill && m && m.elapsed) fill.style.width = `${Math.min(100, state.playTime / m.elapsed * 100)}%`; }
  players().forEach((p, i) => { p.ontimeupdate = () => { if (i === 0 || !$('#audio-mic').getAttribute('src')) { state.playTime = p.currentTime; updatePlaybar(); } }; p.onended = () => { state.playing = false; updatePlaybar(); }; });

  // ── polling ──────────────────────────────────────────────────
  async function tick() {
    await Promise.all([refreshStatus(), refreshList()]);
    if (state.view === 'meeting' && state.meetingId) {
      const editingField = document.activeElement?.matches('input, textarea, select');
      const before = state.detail;
      await refreshDetail();
      if (!state.detail) { render(); return; }
      const captureKey = (c) => c ? [c.state, c.gaps] : null;
      const changed = state.pendingRender || JSON.stringify({ c: captureKey(state.detail.capture), j: state.detail.jobs, p: state.detail.passages.length, s: state.detail.summary && state.detail.summary.id, r: state.detail.meeting.transcript_rev, st: state.detail.meeting.state, a: state.detail.meeting.audio_state })
        !== JSON.stringify({ c: captureKey(before && before.capture), j: before && before.jobs, p: before && before.passages.length, s: before && before.summary && before.summary.id, r: before && before.meeting.transcript_rev, st: before && before.meeting.state, a: before && before.meeting.audio_state });
      if (changed && !editingField && !state.editing) {
        const wasLive = before && before.capture;
        const scroll = $('#doc'), top = scroll?.scrollTop || 0;
        const follow = state.tab === 'transcript' && scroll && scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 80;
        render(); state.pendingRender = false;
        const next = $('#doc'); if (next) next.scrollTop = follow ? next.scrollHeight : top;
        if (wasLive && !live()) loadAudio();
      }
      else if (changed && (editingField || state.editing)) { state.pendingRender = true; }
      else if (live()) { const t = $('.rec-time'); if (t) t.textContent = fmt(live().elapsed); $$('.rec-bar .src').forEach((el, i) => { const key = Object.keys(live().levels)[i]; if (key) el.querySelector('.meter').outerHTML = meter(live().state === 'paused' ? 0 : live().levels[key], 8); }); }
      else if (changed) { renderSidebar(); }
    } else renderSidebar();
    const busy = (state.status && state.status.capturing) || (state.status && state.status.running_job) || runningJob();
    clearTimeout(state.pollTimer); state.pollTimer = setTimeout(tick, busy ? 1000 : 4000);
  }

  async function pollLive() {
    const id = state.meetingId;
    if (id && state.view === 'meeting' && document.visibilityState === 'visible' && (live() || runningJob('transcribe'))) {
      try {
        const update = await get(`/api/meetings/${id}/live`);
        if (id === state.meetingId && state.view === 'meeting') {
          const bar = $('.live-status');
          const html = renderLiveStatus(update.live, update.capture);
          if (bar && html !== state.liveStatusHTML) { bar.outerHTML = html; state.liveStatusHTML = html; }
          const cap = update.capture;
          if (cap) {
            const timer = $('.rec-time'); if (timer) timer.textContent = fmt(cap.elapsed);
            $$('.rec-bar .src').forEach((el, i) => { const key = Object.keys(cap.levels)[i]; const m = el.querySelector('.meter'); if (m && key) m.outerHTML = meter(cap.state === 'paused' ? 0 : cap.levels[key], 8); });
          }
        }
      } catch (_) { /* normal polling reports connection errors */ }
    }
    setTimeout(pollLive, 300);
  }

  // ── boot ─────────────────────────────────────────────────────
  (async () => {
    await Promise.all([refreshStatus(), refreshList()]);
    if (!state.status) { $('#main').innerHTML = '<div class="empty"><h1>Ownkey is not reachable.</h1><p>Open Meetings from the tray again.</p></div>'; return; }
    const cap = state.status.capture;
    if (cap && (cap.state === 'recording' || cap.state === 'paused')) await openMeeting(cap.meeting_id, 'thoughts');
    else if (START_VIEW === 'new') { state.view = 'new'; await loadDevices(); render(); }
    else if (START_MEETING && state.meetings.some((m) => m.id === START_MEETING)) await openMeeting(START_MEETING, START_TAB || undefined);
    else if (state.meetings.length) await openMeeting(state.meetings[0].id, START_TAB || undefined);
    else { state.view = 'empty'; render(); }
    tick();
    pollLive();
  })();
})();
