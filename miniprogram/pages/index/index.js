const { API_BASE_URL } = require('../../config');
const API_ORIGIN = API_BASE_URL.replace(/\/+$/, '');
const apiUrl = (path) => /^https?:\/\//.test(path) ? path : API_ORIGIN + path;

const DOCUMENT_EXTENSIONS = ['doc', 'docx', 'odt', 'ott', 'rtf', 'txt'];
const MEDIA_FORMATS = ['mp3', 'wav', 'flac', 'ogg', 'opus', 'aac', 'm4a', 'mp4', 'webm', 'mkv', 'mov', 'avi'];
const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
const VIDEO_FORMATS = ['mp4', 'webm', 'mkv', 'mov', 'avi'];

function extensionOf(name = '') {
  const point = name.lastIndexOf('.');
  return point < 0 ? '' : name.slice(point + 1).toLowerCase();
}

function formatSize(bytes = 0) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function responseMessage(data, fallback) {
  try {
    const parsed = typeof data === 'string' ? JSON.parse(data) : data;
    return parsed.detail || parsed.message || fallback;
  } catch (_) {
    return fallback;
  }
}

Page({
  data: {
    mode: 'document',
    formats: MEDIA_FORMATS,
    targetIndex: 0,
    file: null,
    uploading: false,
    progress: 0,
    result: null,
    resultKind: '',
    resultTempPath: '',
    savedPath: '',
    activeTab: 'workspace',
    clientId: '',
    historyFiles: [],
    historyLoading: false,
    savedFiles: [],
    apiConfigured: /^https?:\/\/[^/]+$/.test(API_ORIGIN) && !API_ORIGIN.includes('YOUR_API_DOMAIN')
  },

  onLoad() {
    let clientId = wx.getStorageSync('converter_client_id');
    if (!clientId) {
      clientId = 'client-' + Date.now() + '-' + Math.random().toString(36).slice(2, 10);
      wx.setStorageSync('converter_client_id', clientId);
    }
    this.setData({ clientId });
    this.loadSavedFiles();
  },

  switchTab(event) {
    const activeTab = event.currentTarget.dataset.tab || 'workspace';
    this.setData({ activeTab });
    wx.pageScrollTo({ scrollTop: 0, duration: 220 });
    if (activeTab === 'profile' || activeTab === 'history') {
      this.loadHistory();
      this.loadSavedFiles();
    }
  },

  loadHistory() {
    if (!this.data.apiConfigured || !this.data.clientId) return;
    this.setData({ historyLoading: true });
    wx.request({
      url: apiUrl('/api/v1/history/' + encodeURIComponent(this.data.clientId)),
      success: (response) => {
        const files = response.statusCode === 200 && Array.isArray(response.data.files) ? response.data.files : [];
        this.setData({ historyFiles: files });
      },
      fail: () => this.setData({ historyFiles: [] }),
      complete: () => this.setData({ historyLoading: false })
    });
  },

  loadSavedFiles() {
    if (typeof wx.getSavedFileList !== 'function') return;
    wx.getSavedFileList({
      success: ({ fileList }) => this.setData({ savedFiles: fileList || [] }),
      fail: () => this.setData({ savedFiles: [] })
    });
  },

  openHistoryFile(event) {
    const { url, filename } = event.currentTarget.dataset;
    if (!url) return;
    wx.downloadFile({
      url: apiUrl(url),
      success: (response) => {
        if (response.statusCode !== 200) {
          wx.showToast({ title: '文件链接已失效，请重新转换', icon: 'none' });
          return;
        }
        wx.openDocument({
          filePath: response.tempFilePath,
          showMenu: true,
          fileType: extensionOf(filename),
          fail: () => wx.showModal({ title: '无法直接打开', content: '请在打开页面右上角使用系统分享或保存。', showCancel: false })
        });
      },
      fail: () => wx.showToast({ title: '下载失败，请重试', icon: 'none' })
    });
  },

  openSavedFile(event) {
    const filePath = event.currentTarget.dataset.path;
    if (!filePath) return;
    wx.openDocument({
      filePath,
      showMenu: true,
      fileType: extensionOf(filePath),
      fail: () => wx.showToast({ title: '文件已不可用，请重新下载', icon: 'none' })
    });
  },

  chooseMode(event) {
    if (this.data.uploading) return;
    this.setData({ mode: event.currentTarget.dataset.mode, file: null, result: null, resultKind: '', resultTempPath: '', savedPath: '' });
  },

  chooseFile() {
    if (this.data.uploading) return;
    const options = { count: 1, type: 'file' };
    if (this.data.mode === 'document') options.extension = DOCUMENT_EXTENSIONS;

    wx.chooseMessageFile({
      ...options,
      success: ({ tempFiles }) => {
        const file = tempFiles[0];
        if (file.size > MAX_UPLOAD_BYTES) {
          wx.showToast({ title: '文件超过小程序单次上传限制（10 MB）', icon: 'none' });
          return;
        }
        if (this.data.mode === 'document' && !DOCUMENT_EXTENSIONS.includes(extensionOf(file.name))) {
          wx.showToast({ title: '暂不支持此文档格式', icon: 'none' });
          return;
        }
        this.setData({
          file: { ...file, sizeText: formatSize(file.size) },
          result: null,
          resultKind: '',
          resultTempPath: '',
          savedPath: ''
        });
      }
    });
  },

  clearFile() {
    if (!this.data.uploading) this.setData({ file: null, result: null, resultKind: '', resultTempPath: '', savedPath: '' });
  },

  changeFormat(event) {
    this.setData({ targetIndex: Number(event.detail.value), result: null, resultKind: '', resultTempPath: '', savedPath: '' });
  },

  startConversion() {
    if (!this.data.apiConfigured) {
      wx.showToast({ title: '请先配置 API 域名', icon: 'none' });
      return;
    }
    if (!this.data.file) {
      wx.showToast({ title: '请先选择文件', icon: 'none' });
      return;
    }
    if (this.data.uploading) return;

    const endpoint = this.data.mode === 'document'
      ? '/api/v1/convert/document-to-pdf'
      : '/api/v1/convert/media';
    const formData = { response_mode: 'link', client_id: this.data.clientId || 'anonymous' };
    if (this.data.mode === 'media') formData.target_format = this.data.formats[this.data.targetIndex];

    this.setData({ uploading: true, progress: 0, result: null, resultKind: '', resultTempPath: '', savedPath: '' });
    const task = wx.uploadFile({
      url: `${API_ORIGIN}${endpoint}`,
      filePath: this.data.file.path,
      name: 'file',
      formData,
      success: (response) => {
        if (response.statusCode < 200 || response.statusCode >= 300) {
          wx.showToast({ title: responseMessage(response.data, '转换失败'), icon: 'none' });
          return;
        }
        try {
          const result = JSON.parse(response.data);
          if (!result.download_url || !result.filename) throw new Error('invalid response');
          const extension = extensionOf(result.filename);
          const resultKind = extension === 'pdf' ? 'pdf' : VIDEO_FORMATS.includes(extension) ? 'video' : 'audio';
          const expiresMinutes = Math.ceil(Number(result.expires_in || 900) / 60);
          this.setData({ result: { ...result, expiresText: `${expiresMinutes} 分钟` }, resultKind });
        } catch (_) {
          wx.showToast({ title: '服务返回格式异常', icon: 'none' });
        }
      },
      fail: (error) => {
        if (error.errMsg && !error.errMsg.includes('abort')) {
          wx.showToast({ title: '网络连接失败', icon: 'none' });
        }
      },
      complete: () => this.setData({ uploading: false, progress: 0 })
    });
    task.onProgressUpdate(({ progress }) => this.setData({ progress }));
    this.uploadTask = task;
  },

  cancelUpload() {
    if (this.uploadTask && this.data.uploading) this.uploadTask.abort();
  },

  downloadResult(options = {}) {
    if (!this.data.result) return;
    wx.downloadFile({
      url: apiUrl(this.data.result.download_url),
      success: (response) => {
        if (response.statusCode !== 200) {
          wx.showToast({ title: '下载链接已失效，请重新转换', icon: 'none' });
          this.setData({ result: null });
          return;
        }
        this.setData({ resultTempPath: response.tempFilePath }, () => {
          if (options.autoOpen) this.openResult();
          if (options.autoSave) this.saveResult();
        });
      },
      fail: () => wx.showToast({ title: '下载失败，请重试', icon: 'none' })
    });
  },

  openResult() {
    if (!this.data.resultTempPath) {
      this.downloadResult({ autoOpen: true });
      return;
    }
    wx.openDocument({
      filePath: this.data.resultTempPath,
      showMenu: true,
      fileType: extensionOf(this.data.result.filename),
      fail: () => wx.showToast({ title: '无法打开此文件', icon: 'none' })
    });
  },

  saveResult() {
    if (!this.data.resultTempPath) {
      this.downloadResult({ autoSave: true });
      return;
    }
    wx.saveFile({
      tempFilePath: this.data.resultTempPath,
      success: ({ savedFilePath }) => {
        this.setData({ savedPath: savedFilePath });
        this.loadSavedFiles();
        wx.showToast({ title: '已保存到小程序文件', icon: 'success' });
      },
      fail: () => wx.showToast({ title: '保存失败', icon: 'none' })
    });
  }
});
