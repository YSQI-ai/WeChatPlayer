// Harness: run index.js against a stubbed wx runtime to check the flow logic.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const code = fs.readFileSync('miniprogram/pages/index/index.js', 'utf8');

let page = null;
const log = { openDocument: [], toasts: [], downloadFile: [], request: [] };
let downloadBehaviour = 'ok';

const wx = {
  getStorageSync: () => '',
  setStorageSync: () => {},
  showToast: (o) => log.toasts.push(o.title),
  showModal: (o) => log.toasts.push('MODAL:' + o.title),
  pageScrollTo: () => {},
  request: (o) => { log.request.push(o.url); },
  uploadFile: () => ({ onProgressUpdate() {}, abort() {} }),
  downloadFile: (o) => {
    log.downloadFile.push(o.url);
    if (downloadBehaviour === 'ok') o.success({ statusCode: 200, tempFilePath: '/tmp/stub.bin' });
    else o.fail({ errMsg: 'fail' });
  },
  openDocument: (o) => {
    log.openDocument.push(o.fileType);
    // The real API rejects anything that is not a document or PDF.
    if (!['pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx'].includes(o.fileType)) {
      if (o.fail) o.fail({ errMsg: 'openDocument:fail filetype not supported' });
    } else if (o.success) o.success({});
  },
  getSavedFileList: () => {},
  removeSavedFile: () => {},
  saveFile: () => {},
  chooseMessageFile: () => {},
  createInnerAudioContext: () => ({}),
};

const sandbox = {
  wx,
  Page: (o) => { page = o; },
  require: () => ({ API_BASE_URL: 'https://api.example.test' }),
  module: { exports: {} },
  console, setTimeout, clearTimeout,
};
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
assert.ok(page, 'Page() was never called');

function makePage() {
  const inst = Object.create(page);
  inst.data = JSON.parse(JSON.stringify(page.data));
  inst.setData = function (patch, cb) { Object.assign(this.data, patch); if (cb) cb(); };
  return inst;
}

function reset() {
  log.openDocument = []; log.toasts = []; log.downloadFile = []; log.request = [];
  downloadBehaviour = 'ok';
  // Restore defaults; individual checks override these.
  wx.request = (o) => { log.request.push(o.url); };
  wx.downloadFile = (o) => {
    log.downloadFile.push(o.url);
    if (downloadBehaviour === 'ok') o.success({ statusCode: 200, tempFilePath: '/tmp/stub.bin' });
    else o.fail({ errMsg: 'fail' });
  };
}

const results = [];
function check(name, fn) {
  try { reset(); fn(); results.push(['PASS', name]); }
  catch (err) { results.push(['FAIL', name + ' -> ' + err.message]); }
}

// --- openResult must not hand audio/video to wx.openDocument -------------
check('openResult(audio) never calls openDocument', () => {
  const p = makePage();
  p.data.result = { filename: 'song.flac', download_url: '/api/v1/download/T' };
  p.data.resultKind = 'audio';
  p.data.resultTempPath = '/tmp/song.flac';
  p.openResult();
  assert.strictEqual(log.openDocument.length, 0, 'openDocument was called for audio');
  assert.strictEqual(log.toasts.length, 0, 'a spurious toast was shown: ' + log.toasts);
});

check('openResult(pdf) still opens the document', () => {
  const p = makePage();
  p.data.result = { filename: 'doc.pdf', download_url: '/api/v1/download/T' };
  p.data.resultKind = 'pdf';
  p.data.resultTempPath = '/tmp/doc.pdf';
  p.openResult();
  assert.deepStrictEqual(log.openDocument, ['pdf']);
});

check('openResult(video) never calls openDocument', () => {
  const p = makePage();
  p.data.result = { filename: 'clip.mp4', download_url: '/api/v1/download/T' };
  p.data.resultKind = 'video';
  p.data.resultTempPath = '/tmp/clip.mp4';
  p.openResult();
  assert.strictEqual(log.openDocument.length, 0);
  assert.strictEqual(log.toasts.length, 0);
});

// --- history: audio goes to the player, not openDocument -----------------
check('openHistoryFile(audio) loads the player instead of openDocument', () => {
  const p = makePage();
  p.data.historyFiles = [{ download_url: '/api/v1/download/T', filename: 'song.flac', expires_in: 600, size_text: '3.0 MB' }];
  p.openHistoryFile({ currentTarget: { dataset: { url: '/api/v1/download/T', filename: 'song.flac' } } });
  assert.strictEqual(log.openDocument.length, 0, 'openDocument was used for audio history');
  assert.strictEqual(p.data.activeTab, 'workspace');
  assert.strictEqual(p.data.resultKind, 'audio');
  assert.strictEqual(p.data.resultTempPath, '/tmp/stub.bin');
  assert.strictEqual(p.data.result.expiresText, '10 分钟');
});

check('openHistoryFile(pdf) still opens the document', () => {
  const p = makePage();
  p.data.historyFiles = [];
  p.openHistoryFile({ currentTarget: { dataset: { url: '/api/v1/download/T', filename: 'doc.pdf' } } });
  assert.deepStrictEqual(log.openDocument, ['pdf']);
  assert.strictEqual(p.data.activeTab !== 'workspace' || p.data.resultTempPath === '', true);
});

// --- helper queue polling ------------------------------------------------
check('queued response starts polling and shows the waiting card', () => {
  const p = makePage();
  p.startHelperWait({ job_id: 7, status_url: '/api/v1/jobs/7?client_id=c', filename: 'song.flac' });
  assert.strictEqual(p.data.helperJob.jobId, 7);
  assert.strictEqual(p.data.helperJob.status, 'queued');
  p.stopHelperWait();
});

check('a succeeded job becomes a normal result', () => {
  const p = makePage();
  p.data.helperJob = { jobId: 7, statusUrl: '/api/v1/jobs/7?client_id=c', filename: 'song.flac', status: 'leased' };
  p.finishHelperJob({ status: 'succeeded', filename: 'song.flac', download_url: '/api/v1/download/T', size_bytes: 3145728 });
  assert.strictEqual(p.data.helperJob, null);
  assert.strictEqual(p.data.resultKind, 'audio');
  assert.strictEqual(p.data.result.size_text, '3.0 MB');
  assert.strictEqual(p.data.result.download_url, '/api/v1/download/T');
});

check('a failed job reports the server message and stops polling', () => {
  const p = makePage();
  p.data.helperJob = { jobId: 7, statusUrl: '/api/v1/jobs/7?client_id=c', filename: 'song.flac', status: 'queued' };
  p.data.historyFiles = [];
  wx.request = (o) => o.success({ statusCode: 200, data: { status: 'failed', error_message: 'no authorized key source' } });
  p.pollHelperJob();
  assert.strictEqual(p.data.helperJob.status, 'failed');
  assert.strictEqual(p.data.helperJob.message, 'no authorized key source');
  assert.strictEqual(p.helperPollTimer, null, 'polling must stop on a terminal status');
});

check('a still-running job schedules another poll', () => {
  const p = makePage();
  p.data.helperJob = { jobId: 8, statusUrl: '/api/v1/jobs/8?client_id=c', filename: 'song.flac', status: 'queued' };
  p.helperPollAttempts = 0;
  wx.request = (o) => o.success({ statusCode: 200, data: { status: 'leased' } });
  p.pollHelperJob();
  assert.strictEqual(p.data.helperJob.status, 'leased');
  assert.ok(p.helperPollTimer, 'a running job must be polled again');
  p.stopHelperWait();
});

check('a new upload clears a stale helper job', () => {
  const p = makePage();
  p.data.helperJob = { jobId: 6, statusUrl: '/x', status: 'failed', message: 'old' };
  p.data.result = { filename: 'old.flac' };
  p.data.file = { name: 'new.mflac', path: '/tmp/new.mflac' };
  p.data.outputName = 'new';
  p.data.apiConfigured = true;
  p.startConversion();
  assert.strictEqual(p.data.helperJob, null, 'the stale helper card survived a new upload');
  assert.strictEqual(p.data.uploading, true);
});

check('history rows marked helper_queued are not shown as failures', () => {
  const p = makePage();
  const rows = [
    { id: '5', status: 'failed', error_message: 'helper_queued:12', filename: 'a.mflac' },
    { id: 'T', status: 'failed', error_message: 'real failure', filename: 'b.mflac' },
    { id: 'T2', status: 'success', filename: 'c.flac' },
  ];
  p.data.apiConfigured = true;
  p.data.clientId = 'c';
  // normalizeHistoryItem is module-scoped, so exercise it through loadHistory.
  wx.request = (o) => o.success({ statusCode: 200, data: { files: rows } });
  p.loadHistory();
  const captured = p.data.historyFiles;
  assert.strictEqual(captured[0].helper_queued, true);
  assert.strictEqual(captured[1].helper_queued, undefined);
  assert.strictEqual(captured[2].helper_queued, undefined);
  assert.strictEqual(captured[0].job_id, '12');
});

let failed = 0;
for (const [state, name] of results) {
  if (state === 'FAIL') failed++;
  console.log(`  ${state}  ${name}`);
}
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
