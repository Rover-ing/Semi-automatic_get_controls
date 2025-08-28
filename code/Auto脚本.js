(() => {
  const ENDPOINT_DEFAULT = 'http://127.0.0.1:8001/bridge/capture_tap';
  const BOUNDS_RE = /\[(\d+),(\d+)\]\[(\d+),(\d+)\]/;
  let REFRESH_INTERVAL = 500; // ms
  let REFRESH_TIMES = 1;      // 点击次数

  const USE_XPATH = false;
  let armed = false;
  let armedAt = 0;
  const ARM_TIMEOUT = 800;

  // 1) 创建悬浮面板（新增 XPath 优先）
  const panel = document.createElement('div');
  panel.style.cssText = 'position:fixed;right:12px;bottom:12px;z-index:999999;background:#111;color:#fff;padding:10px 12px;border-radius:8px;font:12px/1.4 system-ui,Segoe UI,Arial;box-shadow:0 4px 16px rgba(0,0,0,.3)';
  panel.innerHTML = `
    <div style="margin-bottom:6px;font-weight:600">UIAutodev Bridge</div>
    <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px">
      <label>Endpoint</label>
      <input id="__ep" style="width:280px" value="${ENDPOINT_DEFAULT}">
    </div>
    <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px">
      <label style="min-width:62px">Action</label>
      <select id="__act" style="width:160px">
        <option value="short-click">短点击</option>
        <option value="long-click">长按</option>
        <option value="input">输入</option>
        <option value="swipe">滑动</option>
        <option value="back">系统返回</option>
      </select>
      <label id="__durLabel">时长ms</label>
      <input id="__dur" type="number" value="600" style="width:80px">
    </div>
    <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px">
      <label>XPath</label>
  <input id="__xp" style="width:280px" placeholder=".//node[@resource-id='...']">
  <button id="__grab" title="从页面输入框/面板抓取 XPath">抓取</button>
    </div>
    <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px">
      <label title="优先用此选择器在页面中抓取 XPath">XPath选择器</label>
      <input id="__xpsel" style="width:260px" placeholder="例如: textarea.p-inputtextarea">
      <button id="__saveSel" title="保存选择器">保存</button>
    </div>
    <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px">
      <label>Bounds</label>
      <input id="__bd" style="width:280px" placeholder="[x1,y1][x2,y2]">
      <button id="__send">发送</button>
    </div>
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">
      <label>采集时机</label>
      <select id="__capMode" style="width:120px">
        <option value="mid">进行时</option>
        <option value="post" selected>操作后</option>
      </select>
      <label id="__midDelayLabel">进行时延迟ms</label>
      <input id="__midDelay" type="number" value="50" style="width:90px" title="进行时采集前的短暂延迟，防止截图太快">
      <label id="__waitLabel">正常延时ms</label>
      <input id="__wait" type="number" value="400" style="width:90px" title="动作完成后等待再采集，用于慢加载页面">
    </div>
    <div id="__extra" style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:6px">
      <label>文本</label>
      <input id="__text" placeholder="用于输入动作" style="width:220px">
      <label>dx</label>
      <input id="__dx" type="number" value="0" style="width:80px">
      <label>dy</label>
      <input id="__dy" type="number" value="0" style="width:80px">
      <label>方向</label>
      <select id="__dir" title="滑动方向（指定方向将忽略 dx/dy）">
        <option value="custom">自定义</option>
        <option value="up">上</option>
        <option value="down">下</option>
        <option value="left">左</option>
        <option value="right">右</option>
      </select>
      <label>距离px</label>
      <input id="__dist" type="number" value="0" style="width:90px">
    </div>
    <div style="display:flex;gap:8px;align-items:center">
      <label><input id="__auto" type="checkbox" checked> 自动发送（优先 XPath，缺失则回退 Bounds）</label>
      <button id="__stop" title="停止并截取最终截图">停止</button>
      <span id="__status" style="opacity:.8"></span>
    </div>
  <!-- 已移除：变化需确认 / 锁定选择 / 稳定期ms -->
    <div style="display:flex;gap:8px;align-items:center;margin-top:6px">
      <label>刷新次数</label>
      <input id="__rt" type="number" value="3" style="width:60px">
      <label>间隔ms</label>
      <input id="__ri" type="number" value="500" style="width:80px">
      <button id="__applyR" title="应用刷新参数">应用</button>
    </div>
  `;
  document.body.appendChild(panel);

  // 改动：监听点击后直接发送（忽略稳定时间与武装过期）
  // 过滤：脚本触发(e.isTrusted=false)与点击浮窗(panel.contains)
  // 新增：shouldIgnoreClick，统一忽略浮窗与“刷新按钮”所在区域（无论手动或脚本点击）
  function shouldIgnoreClick(e) {
    try {
      const t = e && e.target;
      if (!t) return false;
      // 1) 悬浮面板内的任何点击都忽略
      if (panel && panel.contains(t)) return true;
      // 2) 刷新按钮及其子元素的点击一律忽略（scheduleRefreshClicks 就是点它）
      const rbtn = findRefreshButton && findRefreshButton();
      if (rbtn && (t === rbtn || (rbtn.contains && rbtn.contains(t)))) return true;
      // 3) 支持手动标记忽略区域：任一祖先含 data-bridge-ignore 或 .bridge-ignore
      if (t.closest && (t.closest('[data-bridge-ignore]') || t.closest('.bridge-ignore'))) return true;
    } catch (_) { }
    return false;
  }

  document.addEventListener('click', (e) => {
    try {
      // 先判断是否属于“固定忽略区域”（无论手动/脚本触发都忽略）
      if (shouldIgnoreClick(e)) return;
      // 再过滤脚本触发的点击（其它位置的程序化点击也不触发）
      if (e && e.isTrusted === false) return;
      sendCurrentSelection(e);
    } catch (_) { }
  }, true);
  const $ep = panel.querySelector('#__ep');
  const $act = panel.querySelector('#__act');
  const $dur = panel.querySelector('#__dur');
  const $xp = panel.querySelector('#__xp');
  const $bd = panel.querySelector('#__bd');
  const $send = panel.querySelector('#__send');
  const $auto = panel.querySelector('#__auto');
  const $stop = panel.querySelector('#__stop');
  const $text = panel.querySelector('#__text');
  const $dx = panel.querySelector('#__dx');
  const $dy = panel.querySelector('#__dy');
  const $dir = panel.querySelector('#__dir');
  const $dist = panel.querySelector('#__dist');
  const $status = panel.querySelector('#__status');
  const $rt = panel.querySelector('#__rt');
  const $ri = panel.querySelector('#__ri');
  const $applyR = panel.querySelector('#__applyR');
  const $xpsel = panel.querySelector('#__xpsel');
  const $saveSel = panel.querySelector('#__saveSel');
  const $wait = panel.querySelector('#__wait');
  const $midDelay = panel.querySelector('#__midDelay');
  const $capMode = panel.querySelector('#__capMode');
  const $durLabel = panel.querySelector('#__durLabel');
  const $midDelayLabel = panel.querySelector('#__midDelayLabel');
  const $waitLabel = panel.querySelector('#__waitLabel');

  // 恢复本地配置
  try {
    const cfg = JSON.parse(localStorage.getItem('UIA_BRIDGE_CFG') || '{}');
    if (cfg.xpathSelector) $xpsel.value = cfg.xpathSelector;
    if (typeof cfg.capMode === 'string' && $capMode) $capMode.value = cfg.capMode;
    if (typeof cfg.midDelayMs === 'number' && $midDelay) $midDelay.value = String(cfg.midDelayMs);
    if (typeof cfg.waitAfterMs === 'number' && $wait) $wait.value = String(cfg.waitAfterMs);
  } catch (_) { }

  function saveCfg(delta = {}) {
    try {
      const cfg = JSON.parse(localStorage.getItem('UIA_BRIDGE_CFG') || '{}');
      const next = Object.assign({}, cfg, delta);
      localStorage.setItem('UIA_BRIDGE_CFG', JSON.stringify(next));
    } catch (_) { }
  }

  // 关闭/隐藏与 XPath 相关的 UI（Bounds 优先策略）
  try {
    const xpRow = $xp && $xp.closest('div');
    const xpselRow = $xpsel && $xpsel.closest('div');
    const grabBtn = panel.querySelector('#__grab');
    if (xpRow) xpRow.style.display = 'none';
    if (xpselRow) xpselRow.style.display = 'none';
    if (grabBtn) grabBtn.style.display = 'none';
  } catch (_) { }

  // 根据动作显示/隐藏额外参数区
  function showPair(inputEl, show) {
    if (!inputEl) return;
    const label = inputEl.previousElementSibling;
    if (label && label.tagName === 'LABEL') label.style.display = show ? '' : 'none';
    inputEl.style.display = show ? '' : 'none';
  }

  function updateExtraFields() {
    const act = ($act.value || 'tap').toLowerCase();
    // 先全部隐藏
    showPair($text, false);
    showPair($dx, false);
    showPair($dy, false);
    showPair($dir, false);
    showPair($dist, false);
    // 时长仅在长按/滑动显示
    const showDur = (act === 'long-click' || act === 'swipe');
    if ($durLabel) $durLabel.style.display = showDur ? '' : 'none';
    if ($dur) $dur.style.display = showDur ? '' : 'none';
    // 再按动作开启
    if (act === 'input') {
      showPair($text, true);
    } else if (act === 'swipe') {
      showPair($dx, true);
      showPair($dy, true);
      showPair($dir, true);
      showPair($dist, true);
    }
    // back 不需要 XPath/Bounds
    const needNode = act !== 'back';
    $xp.disabled = !needNode;
    $bd.disabled = !needNode;
  }
  $act.addEventListener('change', updateExtraFields);
  // 初始化
  updateExtraFields();

  // 采集时机 UI 联动
  function updateCaptureUI() {
    const mode = ($capMode && $capMode.value) || 'post';
    const isMid = mode === 'mid';
    if ($midDelayLabel) $midDelayLabel.style.display = isMid ? '' : 'none';
    if ($midDelay) $midDelay.style.display = isMid ? '' : 'none';
    if ($waitLabel) $waitLabel.style.display = isMid ? 'none' : '';
    if ($wait) $wait.style.display = isMid ? 'none' : '';
  }
  if ($capMode) $capMode.addEventListener('change', updateCaptureUI);
  updateCaptureUI();

  // 2) 取选中节点对象（尽量从全局状态获取）
  function getSelectedNode() {
    const n1 = window._selectedNode;
    const n2 = window.store && window.store.state && window.store.state.selectedNode;
    return n1 || n2 || null;
  }

  // 3) 提取 XPath（已禁用，Bounds 优先）
  function extractXPath() { return ''; }

  // 4) 提取 Bounds（作为回退）
  function extractBounds() {
    // 优先 node 对象
    const node = getSelectedNode();
    if (node) {
      const b1 = node.bounds;
      const b2 = node.attributes && node.attributes.bounds;
      const b = b1 || b2 || '';
      if (typeof b === 'string' && BOUNDS_RE.test(b)) return b;
    }
    // 再从属性/表格里找
    const candidates = Array.from(document.querySelectorAll('table, .el-table, .attributes, .attr, .props, .panel, .right, .detail, .inspector'));
    for (const box of candidates) {
      const txt = box.innerText || '';
      const m = txt.match(BOUNDS_RE);
      if (m) return m[0];
    }
    // 全页兜底
    // const m2 = (document.body.innerText || '').match(BOUNDS_RE);
    // return m2 ? m2[0] : '';
  }

  // 刷新：查找并多次点击页面的“刷新按钮”
  function findRefreshButton() {
    // 常见为：<button> 内含 <span class="p-button-icon pi.pi-refresh">
    const icon = document.querySelector('button .p-button-icon.pi.pi-refresh');
    if (icon && icon.closest) {
      return icon.closest('button');
    }
    // 兜底：包含 pi-refresh 的任意元素向上找 button
    const any = document.querySelector('.pi.pi-refresh, [class*="pi-refresh"]');
    if (any && any.closest) return any.closest('button');
    return null;
  }

  function scheduleRefreshClicks(times = REFRESH_TIMES, delay = REFRESH_INTERVAL) {
    let count = 0;
    const clickOnce = () => {
      const btn = findRefreshButton();
      if (btn) {
        try { btn.click(); } catch (_) { }
      }
      count += 1;
      if (count < times) setTimeout(clickOnce, delay);
    };
    setTimeout(clickOnce, delay);
  }

  // 应用刷新参数
  $applyR.onclick = () => {
    const t = parseInt($rt.value || '3', 10) || 3;
    const i = parseInt($ri.value || '500', 10) || 500;
    REFRESH_TIMES = Math.max(0, t);
    REFRESH_INTERVAL = Math.max(0, i);
    $status.textContent = `刷新参数已应用 times=${REFRESH_TIMES} interval=${REFRESH_INTERVAL}ms`;
  };

  // 保存 XPath 选择器
  if ($saveSel) {
    $saveSel.onclick = () => {
      saveCfg({ xpathSelector: ($xpsel.value || '').trim() });
      $status.textContent = '已保存 XPath 选择器';
    };
  }
  if ($capMode) $capMode.onchange = () => saveCfg({ capMode: ($capMode.value || 'post') });
  if ($wait) $wait.onchange = () => saveCfg({ waitAfterMs: parseInt($wait.value || '400', 10) || 400 });
  if ($midDelay) $midDelay.onchange = () => saveCfg({ midDelayMs: Math.max(0, parseInt($midDelay.value || '50', 10) || 50) });

  // 5) 发送到桥接端（优先 xpath）
  async function postSelection({ xpath, bounds, node }) {
    const url = $ep.value || ENDPOINT_DEFAULT;
    $status.textContent = '发送中…';
    try {
      const action = ($act.value || 'short-click').toLowerCase();
      const durationMs = Math.max(1, parseInt($dur.value || '600', 10) || 600);
      const body = { action, durationMs, tap: true };
      // 采集时机参数
      const mode = ($capMode && $capMode.value) || 'post';
      if (mode === 'mid') {
        body.midCapture = true;
        if ($midDelay) body.midDelayMs = Math.max(0, parseInt($midDelay.value || '50', 10) || 50);
      } else {
        if ($wait) body.waitAfterMs = Math.max(0, parseInt($wait.value || '400', 10) || 400);
      }
      if (action === 'input') body.text = ($text.value || '').toString();
      if (action === 'swipe') {
        body.dx = parseInt($dx.value || '0', 10) || 0;
        body.dy = parseInt($dy.value || '0', 10) || 0;
        body.direction = ($dir.value || 'custom').toLowerCase();
        body.distance = parseInt($dist.value || '0', 10) || 0;
      }
      if (action !== 'back') {
        if (bounds && BOUNDS_RE.test(bounds)) {
          body.bounds = bounds;
        } else {
          $status.textContent = '缺少有效 Bounds（形如 [x1,y1][x2,y2]）';
          return;
        }
      }
      const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      const data = await resp.json().catch(() => ({}));
      if (resp.ok && data && data.ok) {
        const ct = data.capture_timing ? `[${data.capture_timing}]` : '';
        $status.textContent = `OK${ct} elem=${data.elem_id} center=(${data.center?.x},${data.center?.y})`;
        console.log('[Bridge OK]', data);
        // 操作完成后自动点页面刷新按钮 2-3 次
        scheduleRefreshClicks();
      } else {
        $status.textContent = `失败 ${data?.error || resp.status}`;
        console.warn('[Bridge ERR]', data);
      }
    } catch (e) {
      $status.textContent = `异常 ${e}`;
      console.error('[Bridge EXC]', e);
    }
  }

  // 新增：点击后立即发送当前选中信息
  function sendCurrentSelection(triggerEvent) {
    if (!$auto.checked) return; // 仍受“自动发送”开关控制
    const act = ($act.value || 'short-click').toLowerCase();

    // back 动作不需要 bounds
    if (act === 'back') {
      postSelection({ xpath: '', bounds: '', node: getSelectedNode() });
      return;
    }

    const bd = extractBounds();
    if (!bd || !BOUNDS_RE.test(bd)) {
      $status.textContent = '未找到有效 Bounds（请先在 UIAutodev 中选中一个节点）';
      return;
    }

    $bd.value = bd;
    postSelection({ xpath: '', bounds: bd, node: getSelectedNode() });
  }

  // 6) 手动发送按钮
  $send.onclick = () => {
    const bd = ($bd.value || '').trim();
    const act = ($act.value || 'short-click').toLowerCase();
    if (act !== 'back' && !BOUNDS_RE.test(bd)) {
      $status.textContent = '请填写有效的 Bounds（形如 [x1,y1][x2,y2]）';
      return;
    }
    postSelection({ xpath: '', bounds: bd, node: getSelectedNode() });
  };

  // 停止并截取最终截图
  $stop.onclick = async () => {
    try {
      $auto.checked = false;
      const base = ($ep.value || ENDPOINT_DEFAULT);
      const url = base.endsWith('/bridge/capture_tap') ? base.replace('/bridge/capture_tap', '/bridge/final_screenshot') : (new URL('/bridge/final_screenshot', base)).toString();
      const resp = await fetch(url, { method: 'POST' });
      const data = await resp.json().catch(() => ({}));
      if (resp.ok && data && data.ok) {
        $status.textContent = `已停止，最终截图: ${data.file}`;
        // 停止后也刷新几次
        scheduleRefreshClicks();
      } else {
        $status.textContent = `停止/截图失败 ${data?.error || resp.status}`;
      }
    } catch (e) {
      $status.textContent = `停止异常 ${e}`;
    }
  };

  // 7) 自动检测（移除稳定等待与武装机制，改为点击即发）
  // 原 maybeAutoSend 与 MutationObserver 不再需要，注释掉即可
  /*
  let lastSent = '';
  let tick = 0;
  let pendingKey = '';
  let pendingSince = 0;
  function maybeAutoSend() { }
  const mo = new MutationObserver(() => { });
  mo.observe(document.body, { subtree: true, childList: true, characterData: true });
  */

  // 初始化同步一次
  const initBd = extractBounds();
  if (initBd) $bd.value = initBd;
  $status.textContent = '就绪（优先 Bounds）';

  // 额外：定时兜底抓取 XPath（防止 MutationObserver 未触发的场景）
  // XPath 定时抓取已禁用

  // 手动抓取按钮
  const $grab = panel.querySelector('#__grab');
  if ($grab) {
    $grab.onclick = () => {
      const v = extractXPath();
      if (v) {
        $xp.value = v;
        $status.textContent = '已抓取 XPath';
      } else {
        $status.textContent = '未找到 XPath，请手动复制粘贴';
      }
    };
  }
})();