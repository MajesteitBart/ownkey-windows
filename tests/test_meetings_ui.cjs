// Synthetic state only. Exercise the actual renderer without booting a browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

function ui() {
  const source = fs.readFileSync(path.join(__dirname, '../meetings/ui/app.js'), 'utf8');
  const boot = source.lastIndexOf('  (async () => {');
  assert.ok(boot > 0, 'Locate the application boot entry point');
  const context = {
    URLSearchParams, location: { search: '' }, sessionStorage: { getItem: () => null },
    document: { addEventListener() {}, querySelector: (selector) => selector.startsWith('#audio-') ? {} : null },
    window: { addEventListener() {} },
  };
  vm.runInNewContext(source.slice(0, boot) + '\n globalThis.ui = { state, renderBanners, renderSummary };\n})();', context);
  context.ui.state.tab = 'transcript';
  context.ui.state.detail = {
    meeting: { state: 'stopped', audio_state: 'kept' }, capture: null, events: [],
    passages: [{ id: 'mic-000000000000-000', text: 'Synthetic saved passage.' }],
    jobs: [{ kind: 'transcribe', state: 'interrupted', progress: 0.4 }],
  };
  return context.ui;
}

test('partial transcript after restart offers transcription retry', () => {
  const { renderBanners } = ui();
  const html = renderBanners();
  assert.match(html, /Transcription was interrupted/);
  assert.match(html, /data-transcribe/);
  assert.match(html, /Retry/);
  assert.doesNotMatch(html, /Not transcribed yet/);
});

test('running and completed retries remove the interrupted banner', () => {
  const { state, renderBanners } = ui();
  state.detail.jobs.push({ kind: 'transcribe', state: 'running', progress: 0.5 });
  assert.doesNotMatch(renderBanners(), /was interrupted|Retry/);
  state.detail.jobs[1].state = 'done';
  assert.doesNotMatch(renderBanners(), /was interrupted|Retry/);
});

test('recovered interrupted capture stops offering transcription after success', () => {
  const { state, renderBanners } = ui();
  state.detail.meeting.state = 'interrupted';
  assert.match(renderBanners(), /Transcription was interrupted/);
  assert.doesNotMatch(renderBanners(), /Transcribe now/);
  state.detail.jobs.push({ kind: 'transcribe', state: 'done', progress: 1 });
  state.detail.meeting.transcribed_at = 1000;
  assert.doesNotMatch(renderBanners(), /data-transcribe|was interrupted|Retry/);
  state.detail.jobs = [];
  state.detail.meeting.transcribed_at = null;
  assert.match(renderBanners(), /Transcribe now/);
});

test('removed audio does not offer an impossible transcription retry', () => {
  const { state, renderBanners } = ui();
  state.detail.meeting.audio_state = 'removed';
  assert.match(renderBanners(), /Transcription was interrupted/);
  assert.doesNotMatch(renderBanners(), /data-transcribe/);
});

test('interrupted speaker and analysis jobs retain their retry actions', () => {
  const { state, renderBanners } = ui();
  for (const kind of ['speakers', 'summary', 'draft']) {
    state.tab = kind === 'speakers' ? 'transcript' : 'summary';
    state.detail.jobs = [{ kind, state: 'interrupted' }];
    assert.match(renderBanners(), new RegExp(`data-${kind}`));
    assert.match(renderBanners(), /was interrupted/);
  }
});

test('superseded analyses warn and cannot cite reused passage ids', () => {
  const { state, renderSummary } = ui();
  state.detail.meeting.transcript_rev = 2;
  state.detail.passages = [{ id: 'p0001', start: 10, text: 'Synthetic replacement.' }];
  state.detail.summary = { input_rev: 1, content: { overview: 'Old synthetic summary.',
    decisions: [{ text: 'Synthetic decision.', refs: ['p0001'], status: 'decided' }] } };
  state.detail.answers = [{ input_rev: 1, question: 'Synthetic question?', content: 'Old answer.', refs: ['p0001'] }];
  state.detail.drafts = [{ input_rev: 1, content: 'Old synthetic draft.' }];
  const old = renderSummary({});
  assert.match(old, /Outdated answer/);
  assert.match(old, /Outdated draft/);
  assert.match(old, /Regenerate the summary to restore citations/);
  assert.doesNotMatch(old, /data-cite=/);
  for (const a of [state.detail.summary, ...state.detail.answers, ...state.detail.drafts]) a.input_rev = 2;
  const current = renderSummary({});
  assert.doesNotMatch(current, /Outdated answer|Outdated draft|restore citations/);
  assert.match(current, /data-cite="p0001"/);
});
