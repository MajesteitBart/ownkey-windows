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
  vm.runInNewContext(source.slice(0, boot) + '\n globalThis.ui = { state, renderBanners };\n})();', context);
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
