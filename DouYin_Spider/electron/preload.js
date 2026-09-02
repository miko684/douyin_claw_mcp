const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('douyinApp', {
  openLogin: () => ipcRenderer.invoke('open-login'),
  getAuthStatus: () => ipcRenderer.invoke('get-auth-status'),
  crawl: (request) => ipcRenderer.invoke('crawl', request),
  openOutput: (folder) => ipcRenderer.invoke('open-output', folder),
  getAppInfo: () => ipcRenderer.invoke('get-app-info'),
  onBackendEvent: (callback) => ipcRenderer.on('backend-event', (_event, payload) => callback(payload)),
  onAuthState: (callback) => ipcRenderer.on('auth-state', (_event, payload) => callback(payload)),
});
