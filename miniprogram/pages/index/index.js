const { API_BASE_URL } = require('../../config');
const API_ORIGIN = API_BASE_URL.replace(/\/+$/, '');
const apiUrl = (path) => /^https?:\/\//.test(path) ? path : API_ORIGIN + path;

const DOCUMENT_EXTENSIONS = ['doc', 'docx', 'odt', 'ott', 'rtf', 'txt'];
const MEDIA_FORMATS = ['mp3', 'wav', 'flac', 'ogg', 'opus', 'aac', 'm4a', 'mp4', 'webm', 'mkv', 'mov', 'avi'];
const MEDIA_INPUT_EXTENSIONS = ['mp3', 'wav', 'flac', 'ogg', 'opus', 'aac', 'm4a', 'mp4', 'webm', 'mkv', 'mov', 'avi', 'mflac', 'ncm'];
const MAX_UPLOAD_BYTES = 1024 * 1024 * 1024;
const VIDEO_FORMATS = ['mp4', 'webm', 'mkv', 'mov', 'avi'];
const SAVED_FILES_KEY = 'converter_saved_files';

function extensionOf(name = '') {
  const point = name.lastIndexOf('.');
  return point < 0 ? '' : name.slice(point + 1).toLowerCase();
}

function formatSize(bytes = 0) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function stemOf(name = '') {
  const value = String(name).split(/[\\/]/).pop() || '';
  const point = value.lastIndexOf('.');
  return (point > 0 ? value.slice(0, point) : value).trim();
}

function formatSavedTime(timestamp) {
  if (!timestamp) return '保存时间未知';
  const numeric = Number(timestamp);
  const date = new Date(numeric > 100000000000 ? numeric : numeric * 1000);
  if (Number.isNaN(date.getTime())) return '保存时间未知';
  const pad = (value) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
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
    outputName: '',
    outputNameError: '',
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
    cloudFiles: [],
    cloudLoading: false,
    renamingCloudId: '',
    renameName: '',
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
      this.loadCloudFiles();
      this.loadSavedFiles();
    }
  },

  loadHistory() {
    if (!this.data.apiConfigured || !this.data.clientId) return;
    this.setData({ historyLoading: true });
    wx.request({
      url: apiUrl('/api/v1/history/' + encodeURIComponent(this.data.clientId)),
      success: (response) => {
        const files = response.statusCode === 200 && Array.isArray(response.data.files)
          ? response.data.files
          : response.statusCode === 200 && Array.isArray(response.data.items) ? response.data.items : [];
        this.setData({ historyFiles: files });
      },
      fail: () => this.setData({ historyFiles: [] }),
      complete: () => this.setData({ historyLoading: false })
    });
  },

  loadCloudFiles() {
    if (!this.data.apiConfigured || !this.data.clientId) return;
    this.setData({ cloudLoading: true });
    wx.request({
      url: apiUrl('/api/v1/cloud-files/' + encodeURIComponent(this.data.clientId)),
      success: (response) => {
        const files = response.statusCode === 200 && Array.isArray(response.data.files) ? response.data.files : [];
        this.setData({ cloudFiles: files });
      },
      fail: () => this.setData({ cloudFiles: [] }),
      complete: () => this.setData({ cloudLoading: false })
    });
  },

  loadSavedFiles() {
    if (typeof wx.getSavedFileList !== 'function') return;
    const metadata = wx.getStorageSync(SAVED_FILES_KEY) || [];
    const metadataByPath = metadata.reduce((map, item) => {
      if (item && item.filePath) map[item.filePath] = item;
      return map;
    }, {});
    wx.getSavedFileList({
      success: ({ fileList }) => {
        const savedFiles = (fileList || []).map((item) => {
          const saved = metadataByPath[item.filePath] || {};
          return {
            ...item,
            filename: saved.filename || saved.originalName || item.filePath.split('/').pop() || '小程序文件',
            sizeText: saved.sizeText || formatSize(item.size || 0),
            createdText: saved.createdText || formatSavedTime(item.createTime)
          };
        });
        this.setData({ savedFiles });
      },
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

  deleteHistoryFile(event) {
    const token = event.currentTarget.dataset.id;
    if (!token || !this.data.clientId) return;
    wx.showModal({
      title: '删除云端文件',
      content: '删除后将无法通过历史记录再次下载，确认删除吗？',
      confirmText: '删除',
      confirmColor: '#c55d6b',
      success: ({ confirm }) => {
        if (!confirm) return;
        wx.request({
          url: apiUrl(`/api/v1/cloud-files/${encodeURIComponent(this.data.clientId)}/${encodeURIComponent(token)}`),
          method: 'DELETE',
          success: (response) => {
            if (response.statusCode !== 200) {
              wx.showToast({ title: responseMessage(response.data, '删除失败，请重试'), icon: 'none' });
              return;
            }
            this.loadHistory();
            this.loadCloudFiles();
            wx.showToast({ title: '云端文件已删除', icon: 'success' });
          },
          fail: () => wx.showToast({ title: '网络连接失败，请重试', icon: 'none' })
        });
      }
    });
  },

  saveToCloud() {
    const result = this.data.result;
    if (!result || !result.download_url || !this.data.clientId) return;
    if (result.cloud_saved) {
      wx.showToast({ title: '文件已在云盘中', icon: 'none' });
      return;
    }
    wx.request({
      url: apiUrl(`/api/v1/cloud-files/${encodeURIComponent(this.data.clientId)}/${encodeURIComponent(result.download_url.split('/').pop())}`),
      method: 'POST',
      success: (response) => {
        if (response.statusCode !== 200) {
          wx.showToast({ title: responseMessage(response.data, '保存到云盘失败'), icon: 'none' });
          return;
        }
        this.setData({ result: { ...result, cloud_saved: true } });
        this.loadHistory();
        this.loadCloudFiles();
        wx.showToast({ title: '已保存到云盘', icon: 'success' });
      },
      fail: () => wx.showToast({ title: '网络连接失败，请重试', icon: 'none' })
    });
  },

  startRenameCloud(event) {
    const { id, filename } = event.currentTarget.dataset;
    if (!id) return;
    const point = String(filename || '').lastIndexOf('.');
    this.setData({ renamingCloudId: id, renameName: point > 0 ? filename.slice(0, point) : filename });
  },

  changeRenameName(event) {
    this.setData({ renameName: (event.detail.value || '').slice(0, 120) });
  },

  cancelRenameCloud() {
    this.setData({ renamingCloudId: '', renameName: '' });
  },

  confirmRenameCloud(event) {
    const token = event.currentTarget.dataset.id;
    const filename = (this.data.renameName || '').trim();
    if (!token || !filename) {
      wx.showToast({ title: '请输入文件名', icon: 'none' });
      return;
    }
    wx.request({
      url: apiUrl(`/api/v1/cloud-files/${encodeURIComponent(this.data.clientId)}/${encodeURIComponent(token)}?filename=${encodeURIComponent(filename)}`),
      method: 'PATCH',
      success: (response) => {
        if (response.statusCode !== 200) {
          wx.showToast({ title: responseMessage(response.data, '重命名失败'), icon: 'none' });
          return;
        }
        this.setData({ renamingCloudId: '', renameName: '' });
        this.loadCloudFiles();
        this.loadHistory();
        wx.showToast({ title: '云盘文件已重命名', icon: 'success' });
      },
      fail: () => wx.showToast({ title: '网络连接失败，请重试', icon: 'none' })
    });
  },

  openSavedFile(event) {
    const { path: filePath, filename } = event.currentTarget.dataset;
    if (!filePath) return;
    wx.openDocument({
      filePath,
      showMenu: true,
      fileType: extensionOf(filename || filePath),
      fail: () => wx.showToast({ title: '文件已不可用，请重新下载', icon: 'none' })
    });
  },

  deleteSavedFile(event) {
    const filePath = event.currentTarget.dataset.path;
    if (!filePath) return;
    wx.removeSavedFile({
      filePath,
      success: () => {
        const metadata = (wx.getStorageSync(SAVED_FILES_KEY) || []).filter((item) => item.filePath !== filePath);
        wx.setStorageSync(SAVED_FILES_KEY, metadata);
        if (this.data.savedPath === filePath) this.setData({ savedPath: '' });
        this.loadSavedFiles();
        wx.showToast({ title: '已删除本地文件', icon: 'success' });
      },
      fail: () => wx.showToast({ title: '删除失败，请重试', icon: 'none' })
    });
  },

  chooseMode(event) {
    if (this.data.uploading) return;
    this.setData({ mode: event.currentTarget.dataset.mode, file: null, outputName: '', outputNameError: '', result: null, resultKind: '', resultTempPath: '', savedPath: '' });
  },

  chooseFile() {
    if (this.data.uploading) return;
    const options = { count: 1, type: 'file' };
    options.extension = this.data.mode === 'document' ? DOCUMENT_EXTENSIONS : MEDIA_INPUT_EXTENSIONS;

    wx.chooseMessageFile({
      ...options,
      success: ({ tempFiles }) => {
        const file = tempFiles[0];
        if (file.size > MAX_UPLOAD_BYTES) {
          wx.showToast({ title: '文件超过单次上传限制（1 GB）', icon: 'none' });
          return;
        }
        if (this.data.mode === 'document' && !DOCUMENT_EXTENSIONS.includes(extensionOf(file.name))) {
          wx.showToast({ title: '暂不支持此文档格式', icon: 'none' });
          return;
        }
        if (this.data.mode === 'media' && !MEDIA_INPUT_EXTENSIONS.includes(extensionOf(file.name))) {
          wx.showToast({ title: '暂不支持此音视频格式', icon: 'none' });
          return;
        }
        const selectedExtension = extensionOf(file.name);
        this.setData({
          file: { ...file, sizeText: formatSize(file.size) },
          outputName: stemOf(file.name),
          targetIndex: ['mflac', 'ncm'].includes(selectedExtension) ? MEDIA_FORMATS.indexOf('flac') : this.data.targetIndex,
          outputNameError: '',
          result: null,
          resultKind: '',
          resultTempPath: '',
          savedPath: ''
        });
      }
    });
  },

  clearFile() {
    if (!this.data.uploading) this.setData({ file: null, outputName: '', outputNameError: '', result: null, resultKind: '', resultTempPath: '', savedPath: '' });
  },

  changeOutputName(event) {
    const outputName = (event.detail.value || '').slice(0, 120);
    this.setData({ outputName, outputNameError: '' });
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
    const outputName = (this.data.outputName || '').trim();
    if (!outputName) {
      this.setData({ outputNameError: '请输入输出文件名' });
      wx.showToast({ title: '请输入输出文件名', icon: 'none' });
      return;
    }
    if (/[\\/:*?"<>|]/.test(outputName)) {
      this.setData({ outputNameError: '文件名不能包含 \\/ : * ? " < > |' });
      wx.showToast({ title: '文件名包含非法字符', icon: 'none' });
      return;
    }
    if (this.data.uploading) return;

    const endpoint = this.data.mode === 'document'
      ? '/api/v1/convert/document-to-pdf'
      : '/api/v1/convert/media';
    const formData = { response_mode: 'link', client_id: this.data.clientId || 'anonymous', output_name: outputName };
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
          this.loadHistory();
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
    if (this.data.savedPath) {
      this.openSavedFile({ currentTarget: { dataset: { path: this.data.savedPath, filename: this.data.result && this.data.result.filename } } });
      return;
    }
    if (!this.data.resultTempPath) {
      this.downloadResult({ autoOpen: true });
      return;
    }
    wx.openDocument({
      filePath: this.data.resultTempPath,
      showMenu: true,
      fileType: extensionOf(this.data.result.filename),
      success: () => {},
      fail: () => wx.showToast({ title: '无法打开此文件，请重新下载', icon: 'none' })
    });
  },

  saveResult(options = {}) {
    if (this.data.savedPath) {
      if (options.autoOpen) this.openSavedFile({ currentTarget: { dataset: { path: this.data.savedPath, filename: this.data.result && this.data.result.filename } } });
      wx.showToast({ title: '文件已保存，可直接打开', icon: 'none' });
      return;
    }
    if (!this.data.resultTempPath) {
      this.downloadResult({ autoSave: true });
      return;
    }
    const existing = (this.data.savedFiles || []).find((item) => item.filename === this.data.result.filename);
    if (existing) {
      this.setData({ savedPath: existing.filePath });
      if (options.autoOpen) this.openSavedFile({ currentTarget: { dataset: { path: existing.filePath, filename: existing.filename } } });
      wx.showToast({ title: '文件已存在，可直接打开', icon: 'none' });
      return;
    }
    wx.saveFile({
      tempFilePath: this.data.resultTempPath,
      success: ({ savedFilePath }) => {
        const result = this.data.result || {};
        const metadata = (wx.getStorageSync(SAVED_FILES_KEY) || []).filter((item) => item.filePath !== savedFilePath && item.filename !== result.filename);
        metadata.push({
          filePath: savedFilePath,
          filename: result.filename || '小程序文件',
          originalName: this.data.file && this.data.file.name,
          size: result.size_bytes || 0,
          sizeText: result.size_text || formatSize(result.size_bytes || 0),
          createdAt: Date.now(),
          createdText: formatSavedTime(Date.now())
        });
        wx.setStorageSync(SAVED_FILES_KEY, metadata);
        this.setData({ savedPath: savedFilePath });
        this.loadSavedFiles();
        if (options.autoOpen) this.openSavedFile({ currentTarget: { dataset: { path: savedFilePath, filename: result.filename } } });
        wx.showToast({ title: '已保存到小程序文件', icon: 'success' });
      },
      fail: (error) => {
        if (error && error.errMsg && /exist|saved|duplicate/i.test(error.errMsg)) {
          wx.showToast({ title: '文件已存在，可在“我的”中打开', icon: 'none' });
          this.loadSavedFiles();
          return;
        }
        wx.showToast({ title: '保存失败，请重试', icon: 'none' });
      }
    });
  },

  deleteSavedResult() {
    if (!this.data.savedPath) return;
    this.deleteSavedFile({ currentTarget: { dataset: { path: this.data.savedPath } } });
  }
});
