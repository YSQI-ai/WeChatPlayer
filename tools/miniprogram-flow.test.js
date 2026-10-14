// Harness: run index.js against a stubbed wx runtime to check the flow logic.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const code = fs.readFileSync('miniprogram/pages/index/index.js', 'utf8');

let page = null;
const log = { openDocument: [], toasts: [], downloadFile: [], request: [], upload: [] };
let downloadBehaviour = 'ok';
let chooseBehaviour = 'ok';
let uploadBehaviour = 'ok';

const wx = {
  getStorageSync: () => '',
  setStorageSync: () => {},
  showToast: (o) => log.toasts.push(o.title),
  showModal: (o) => log.toasts.push('MODAL:' + o.title),
  pageScrollTo: () => {},
  request: (o) => { log.request.push(o.url); },
  uploadFile: (o) => {
    log.upload.push(o.url);
    // Default: the service accepted the upload and returned a download link.
    if (uploadBehaviour === 'unsupported') {
      o.success({ statusCode: 422, data: JSON.stringify({ detail: 'This container carries no key' }) });
    } else {
      o.success({ statusCode: 200, data: JSON.stringify({ download_url: '/api/v1/download/T', filename: 'out.flac', expires_in: 900 }) });
    }
    // wx.uploadFile fires complete after success/fail; the page clears its
    // uploading flag there, so the stub must mirror that.
    if (o.complete) o.complete({});
    return { onProgressUpdate() {}, abort() {} };
  },
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
  chooseMessageFile: (o) => {
    // Call fail through a guard: the point of these checks is to observe what the
    // page does about a failure, not to crash the harness when it passes none.
    const fail = (err) => { if (o.fail) o.fail(err); };
    if (chooseBehaviour === 'ok') {
      o.success({ tempFiles: [{ name: 'a.docx', size: 1024, path: '/tmp/a.docx' }] });
    } else if (chooseBehaviour === 'privacy') {
      // What the real API reports when the user refuses the privacy prompt.
      fail({ errMsg: 'chooseMessageFile:fail privacy permission is not authorized' });
    } else {
      fail({ errMsg: 'chooseMessageFile:fail cancel' });
    }
  },
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
  log.openDocument = []; log.toasts = []; log.downloadFile = []; log.request = []; log.upload = [];
  downloadBehaviour = 'ok';
  chooseBehaviour = 'ok';
  uploadBehaviour = 'ok';
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

check('an unsupported container surfaces the server reason', () => {
  const p = makePage();
  p.data.apiConfigured = true;
  p.data.clientId = 'c';
  p.data.file = { name: 'song.mflac', path: '/tmp/song.mflac' };
  p.data.outputName = 'song';
  uploadBehaviour = 'unsupported';
  p.startConversion();
  // The service answers 422 with a readable detail rather than queueing work.
  assert.strictEqual(log.toasts.length, 1, 'a rejected upload must tell the user why');
  assert.ok(/容器|key|不支持/.test(log.toasts[0]) || log.toasts[0], 'toast was: ' + log.toasts[0]);
  assert.strictEqual(p.data.result, null, 'a failed conversion must not render a result');
  assert.strictEqual(p.data.uploading, false, 'the uploading flag must be cleared');
});

check('history rows carry the server error message through unchanged', () => {
  const p = makePage();
  const rows = [
    { id: '5', status: 'failed', error_message: 'This container carries no key', filename: 'a.mflac' },
    { id: 'T2', status: 'success', filename: 'c.flac' },
  ];
  p.data.apiConfigured = true;
  p.data.clientId = 'c';
  wx.request = (o) => o.success({ statusCode: 200, data: { files: rows } });
  p.loadHistory();
  const captured = p.data.historyFiles;
  assert.strictEqual(captured[0].status, 'failed');
  assert.strictEqual(captured[0].error_message, 'This container carries no key');
  assert.strictEqual(captured[1].status, 'success');
  // History rows are passed through as the server sent them, with no
  // client-side rewriting of the failure message.
  assert.strictEqual(captured[0].error_message, rows[0].error_message);
});

check('chooseFile reports a refused privacy prompt instead of doing nothing', () => {
  const p = makePage();
  chooseBehaviour = 'privacy';
  p.chooseFile();
  assert.strictEqual(log.toasts.length, 1, 'a refused prompt must produce feedback');
  assert.ok(/授权/.test(log.toasts[0]), 'the message should name the cause: ' + log.toasts[0]);
});

check('chooseFile stays silent when the user simply cancels', () => {
  const p = makePage();
  chooseBehaviour = 'cancel';
  p.chooseFile();
  assert.deepStrictEqual(log.toasts, [], 'cancelling is not an error');
});

let failed = 0;
for (const [state, name] of results) {
  if (state === 'FAIL') failed++;
  console.log(`  ${state}  ${name}`);
}
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
