// 在 weditor 页面控制台运行，或作为浏览器书签脚本使用。
// 功能：读取当前选中节点的 bounds/xpath，POST 给桥接服务 /bridge/capture_tap。
// 使用前请先启动：python .\adbgetevent\bridge_server.py --host 127.0.0.1 --port 8001 --reset-json

(async function () {
    const BRIDGE = localStorage.getItem('BRIDGE_BASE') || 'http://127.0.0.1:8001';

    // 自动刷新：查找并多次点击页面中的“刷新”按钮
    function findRefreshButton() {
        const icon = document.querySelector('button .p-button-icon.pi.pi-refresh');
        if (icon && icon.closest) return icon.closest('button');
        const any = document.querySelector('.pi.pi-refresh, [class*="pi-refresh"]');
        if (any && any.closest) return any.closest('button');
        return null;
    }
    function scheduleRefreshClicks(times = 3, delay = 500) {
        let count = 0;
        const clickOnce = () => {
            const btn = findRefreshButton();
            if (btn) { try { btn.click(); } catch (_) { } }
            count += 1;
            if (count < times) setTimeout(clickOnce, delay);
        };
        setTimeout(clickOnce, delay);
    }

    function pickBounds(node) {
        // UIAutomator XML 节点属性名可能为 bounds 或 attributes.bounds
        if (!node) return null;
        if (typeof node.bounds === 'string' && /\[\d+,\d+\]\[\d+,\d+\]/.test(node.bounds)) return node.bounds;
        if (node.attributes && typeof node.attributes.bounds === 'string') return node.attributes.bounds;
        return null;
    }

    // 试探 weditor 的全局变量/面板数据
    let node = null;
    // 1) 尝试 window._selectedNode 或 window.store/state
    node = (window._selectedNode) || (window.store && window.store.state && window.store.state.selectedNode) || null;

    // 2) 如果界面提供节点 JSON 面板，尝试从 DOM 中解析（不同版本可能差异很大，这里仅兜底示例）
    if (!node) {
        try {
            const pre = document.querySelector('pre, code');
            if (pre) {
                const text = pre.innerText || pre.textContent || '';
                try { node = JSON.parse(text); } catch (_) { }
            }
        } catch (_) { }
    }

    if (!node) {
        console.warn('[autosend] 未能定位到所选节点对象，请先在 weditor 中选中一个控件。');
        return;
    }

    const bounds = pickBounds(node);
    let xpath = node.xpath || (node.attributes && node.attributes.xpath) || '';
    if (!xpath) {
        // 从页面的输入框/文本域兜底读取（如 p-inputtextarea）
        const isXPathText = (s) => {
            if (!s) return false; const v = s.toString().trim();
            return /^(\/|\.\/|\.\/\/|\/\/)/.test(v) || v.includes('/hierarchy') || v.startsWith('.//');
        };
        const candidates = Array.from(document.querySelectorAll('textarea, input[type="text"], .p-inputtextarea, [data-pc-name="textarea"]'));
        for (const el of candidates) {
            const val = (('value' in el && typeof el.value === 'string') ? el.value : (el.textContent || '')).trim();
            if (isXPathText(val)) { xpath = val; break; }
        }
    }
    if (!bounds && !xpath) {
        console.warn('[autosend] 既没有 bounds 也没有 xpath，无法发送。');
        return;
    }

    const body = { bounds, xpath, node, tap: true };
    console.log('[autosend] POST ->', BRIDGE + '/bridge/capture_tap', body);

    try {
        const resp = await fetch(BRIDGE + '/bridge/capture_tap', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (!resp.ok || !data.ok) {
            console.error('[autosend] 失败', data);
        } else {
            console.log('[autosend] 成功', data);
            // 操作完成后自动刷新 2-3 次
            scheduleRefreshClicks();
        }
    } catch (err) {
        console.error('[autosend] 网络/跨域错误', err);
    }
})();
