// 配置窗口 preload：仅暴露受控的读写配置能力
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('klConfig', {
  load: () => ipcRenderer.invoke('config:load'),
  save: (form) => ipcRenderer.invoke('config:save', form),
});
