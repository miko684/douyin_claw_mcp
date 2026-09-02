const $ = (selector) => document.querySelector(selector);
const state = { mode: 'search', loggedIn: false, dataDir: '' };

function addLog(message, kind = 'system') {
  const output = $('#log-output');
  const line = document.createElement('div'); line.className = `log-line ${kind}`;
  const now = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  line.innerHTML = `<span>${now}</span><b>${kind === 'error' ? '注意' : kind === 'success' ? '完成' : '记录'}</b><em></em>`;
  line.querySelector('em').textContent = message; output.appendChild(line); output.scrollTop = output.scrollHeight;
}

function setLoggedIn(authenticated, extra = {}) {
  state.loggedIn = authenticated;
  $('#auth-copy').textContent = authenticated ? '抖音已登录，可开始采集' : '还没有登录抖音';
  $('#login-button-text').textContent = authenticated ? '重新扫码登录' : '扫码登录';
  $('#route-login').classList.toggle('done', authenticated); $('#route-login').classList.toggle('active', !authenticated);
  $('#route-login .route-node').textContent = authenticated ? '✓' : '02';
  $('#route-login small').textContent = authenticated ? `${extra.cookieCount || '已'} 个会话字段已接入` : '等待账号会话';
  if (authenticated) { $('#route-crawl').classList.add('active'); $('#route-crawl small').textContent = '可以开始采集'; }
}

function setMode(mode) {
  state.mode = mode; document.querySelectorAll('.mode-tab').forEach((tab) => tab.classList.toggle('active', tab.dataset.mode === mode));
  const search = mode === 'search'; $('#query-label').textContent = search ? '搜索关键词' : mode === 'work' ? '作品链接' : '用户主页链接';
  $('#query').placeholder = search ? '例如：城市骑行、咖啡器具' : mode === 'work' ? '粘贴 https://www.douyin.com/video/...' : '粘贴 https://www.douyin.com/user/...';
  $('#search-options').classList.toggle('hidden', !search); $('#query').value = '';
}

async function refreshAuth() { try { setLoggedIn((await window.douyinApp.getAuthStatus()).authenticated); } catch (error) { addLog(`引擎连接失败：${error.message}`, 'error'); } }
document.querySelectorAll('.mode-tab').forEach((tab) => tab.addEventListener('click', () => setMode(tab.dataset.mode)));
$('#login-button').addEventListener('click', async () => { addLog('正在打开抖音登录窗口…'); await window.douyinApp.openLogin(); });
$('#open-output').addEventListener('click', () => window.douyinApp.openOutput(state.dataDir));
$('#clear-log').addEventListener('click', () => { $('#log-output').innerHTML = ''; });

$('#crawl-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!state.loggedIn) { $('#form-message').textContent = '请先扫码登录抖音'; addLog('采集前需要先完成扫码登录。', 'error'); return; }
  const button = $('#crawl-button'); button.disabled = true; $('#form-message').textContent = '任务运行中，请查看下方日志…'; addLog('采集任务已启动。');
  try {
    const payload = { mode: state.mode, query: $('#query').value.trim(), url: $('#query').value.trim(), requireNum: $('#require-num').value, sortType: $('#sort-type').value, saveChoice: document.querySelector('input[name="save"]:checked').value };
    const result = await window.douyinApp.crawl(payload); state.dataDir = result.outputDir || state.dataDir;
    $('#form-message').textContent = result.message; $('#route-crawl small').textContent = '最近一次任务已完成'; addLog(`${result.message}，结果已保存到本机。`, 'success');
  } catch (error) { $('#form-message').textContent = error.message; addLog(error.message, 'error'); }
  finally { button.disabled = false; }
});

window.douyinApp.onBackendEvent((event) => {
  if (event.event === 'ready') { $('#engine-pill').className = 'pill'; $('#engine-pill').innerHTML = '<i></i>引擎已连接'; state.dataDir = event.dataDir || state.dataDir; $('#data-path').textContent = state.dataDir; addLog('本地采集引擎已就绪。'); }
  else if (event.event === 'log') addLog(event.message);
  else if (event.event === 'error') addLog(event.message, 'error');
});
window.douyinApp.onAuthState((auth) => { setLoggedIn(true, auth); addLog('扫码登录成功，Cookie 已由本地工具接入。', 'success'); });
window.douyinApp.getAppInfo().then((info) => { state.dataDir = info.dataDir; $('#data-path').textContent = info.dataDir; });
refreshAuth();
