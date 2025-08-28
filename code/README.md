# Android UI 采集与桥接使用说明（完整指南）

本项目提供两种采集模式：
<!-- - 事件监听模式：在 PC 侧监听设备触摸事件（adb getevent），每次点击自动采集“点击前”的 XML 与截图并标注。(缺少控件定位，暂时取消，现主要使用桥接模式) -->
- 桥接模式（UIAutodev/weditor 联动）：在网页端点选控件后，由本服务完成“点击前快照 → 标注 → 写 JSON → 中心点击”。支持 XPath 优先、bounds 兜底。

## 一、启动前的准备（必读）
- Android 设备
  - 已开启 开发者选项 和 USB 调试
  - 首次连接 PC 时，设备上弹出的 RSA 授权已勾选“始终允许”并点击“允许”
  - 屏幕保持点亮且解锁
- PC 环境
  - Windows/macOS/Linux，已安装 ADB 并加入 PATH（PowerShell 输入 `adb version` 可看到版本）
  - Python 3.9+ 建议
- 依赖安装（PowerShell）：
  ```powershell
  python -m pip install --upgrade pip ; pip install pillow uiautomator2; pip install uiautodev; pip install flask
  ```
- 驱动/连接检查（Windows 常见）
  - 若 `adb devices` 无法看到设备，安装厂商 USB 驱动或通过“设备管理器”更新驱动
  - 如遇异常可尝试：
    ```powershell
    adb kill-server ; adb start-server ; adb devices
    ```

可用环境变量（按需）：
- `ADB_LISTENER_VERBOSE=1`（事件监听模式）输出调试日志
- `ADB_EVENT_DEVICE=/dev/input/eventX` 强制指定触摸设备
- `BRIDGE_DEBUG=1`（桥接模式）响应体附带错误堆栈，并写入日志

## 二、输出目录与数据结构
- 所有产物位于：`adbgetevent/UI_Automated_acquisition/`
  - `image/elem_N_raw.png`：点击前原始截图
  - `image/elem_N_boxed.png`：带红框标注截图（如命中控件）
  - `element_xml/elem_N.xml`：点击前的控件树 XML（与 N 对齐）
  - `collected_data.json`：采集记录数组（每次启动会被重置为空数组）

JSON 单条记录示例：
```json
{
  "elem_id": "elem_0",
  "time": "2025-08-15T12:34:56",
  "click": { "x": 123, "y": 456 },
  "node": {
    "bounds": "[x1,y1][x2,y2]",
    "text": "...",
    "class": "...",
    "resource-id": "...",
    "content-desc": "...",
    "package": "..."
  },
  "images": {
    "raw": ".../elem_0_raw.png",
    "boxed": ".../elem_0_boxed.png"
  },
  "xml": ".../element_xml/elem_0.xml",
  "activity": "com.pkg/.MainActivity",
  "action": "..."
}
```

<!-- ## 三、事件监听模式（本地自动抓取）
适合直接在设备上手动点击，PC 端自动捕获“点击前”的 UI 快照并标注。

启动：
```powershell
cd .\adbgetevent
python .\run_collector_adb.py
```
提示：
- 脚本会尝试自动识别触摸设备；也可通过 `ADB_EVENT_DEVICE=/dev/input/eventX` 指定
- 每次检测到“抬起”即视为一次点击事件，使用上一轮预采集的 XML+截图进行标注
- Ctrl+C 退出时会保存一张最终截图 -->

## 四、桥接模式（与 UIAutodev/weditor 联动）
用于在网页端选定控件后，一键完成“点击前快照 → 标注 → 写 JSON → 中心点击”。
强烈建议在与 UIAutodev 后端并存时使用 uiautomator2 后端（避免 adb 的 `uiautomator dump` 通道冲突）。


1. 启动uiautodev
```启动uiautodev
python -m uiautodev
```

若uiautodev页面无法进入国内版本，请翻墙进入https://uiauto.dev/

2. 新建终端启动桥接服务：
```powershell
$env:BRIDGE_DEBUG="1"
python .\adbgetevent\bridge_server.py --host 127.0.0.1 --port 8001 --reset-json --backend u2
```
健康检查：
```powershell
Invoke-RestMethod http://127.0.0.1:8001/health
```

3. 来到uiautodev界面F12打开控制台输入Auto脚本内容即可开始采集




前端请求体（两选一，XPath 优先）：
- XPath：`{"xpath": ".//node[@resource-id='id']", "node": {...}, "tap": true}`
- Bounds：`{"bounds": "[x1,y1][x2,y2]", "node": {...}, "tap": true}`

可选动作与参数：
- `action`: `tap` | `long_press` | `input` | `swipe` | `back`（默认 `tap`）
- `durationMs`: 动作时长（ms），用于 `long_press` 与 `swipe`
- `text`: 文本，用于 `input`（会先轻点聚焦再输入）
- `dx`/`dy`: 滑动的位移（像素），用于 `swipe`
- `direction`/`distance`: 当提供方向（up/down/left/right）与距离时，`swipe` 将忽略 `dx/dy`，从元素中心按指定方向滑动对应像素
- `tap`: 是否执行轻点（默认 true）。在 `input` 动作内用于“先点后输”。`back` 动作不需要 `bounds/xpath` 且不写入 JSON 记录，仅返回状态并附带一张返回后的原始截图（post_back_raw.png）。

说明：
- 若提供 xpath，则桥接在“当前 XML”内查找该节点并使用其 bounds 标注与点击；内部对以 `//` 或 `/` 开头的表达式会自动规范为相对 `.//`/`./`
- `node` 为可选的属性兜底（text/class/resource-id 等），写入 JSON 中的 node 字段
- `tap`（默认 true）为是否在采集后对中心坐标执行点击

PowerShell 调试示例：
```powershell
# 以 bounds 调用
$body = @{ bounds = "[100,200][300,260]" } | ConvertTo-Json
Invoke-RestMethod -Uri http://127.0.0.1:8001/bridge/capture_tap -Method Post -ContentType 'application/json' -Body $body

# 以 xpath 调用
$body = @{ xpath = ".//node[@text='设置']" } | ConvertTo-Json
Invoke-RestMethod -Uri http://127.0.0.1:8001/bridge/capture_tap -Method Post -ContentType 'application/json' -Body $body

# 指定 swipe 方向与距离
$body = @{ xpath = ".//node[@text='列表']"; action = "swipe"; direction = "up"; distance = 400; durationMs = 800 } | ConvertTo-Json
Invoke-RestMethod -Uri http://127.0.0.1:8001/bridge/capture_tap -Method Post -ContentType 'application/json' -Body $body

# 输入文本（自动先轻点聚焦后输入）
$body = @{ xpath = ".//node[@resource-id='com.demo:id/edit']"; action = "input"; text = "hello world" } | ConvertTo-Json
Invoke-RestMethod -Uri http://127.0.0.1:8001/bridge/capture_tap -Method Post -ContentType 'application/json' -Body $body

# 系统返回（不生成 JSON 记录）
$body = @{ action = "back" } | ConvertTo-Json
Invoke-RestMethod -Uri http://127.0.0.1:8001/bridge/capture_tap -Method Post -ContentType 'application/json' -Body $body

# 截取最终截图
Invoke-RestMethod -Uri http://127.0.0.1:8001/bridge/final_screenshot -Method Post
```

在 weditor 页面快速注入脚本（自动从页面读取 XPath/Bounds 并上报）：
```js
localStorage.setItem('BRIDGE_BASE', 'http://127.0.0.1:8001');
var s=document.createElement('script');
s.src='http://127.0.0.1:8001/autosend.js';
document.head.appendChild(s);
```
若使用自带的悬浮面板脚本，请将 `Auto脚本.txt` 的内容粘贴到浏览器控制台执行。新版已支持“XPath 优先，Bounds 兜底”。

## 五、常见问题与排障
- 502 Bad Gateway / 预采集失败（日志含“uiautomator dump 失败”）
  - 多发生于 UIAutodev 后端占用 uiautomator 通道。解决：以 `--backend u2` 启动桥接。
- 503 设备未就绪
  - 检查 `adb devices` 是否有 `device` 状态；确认 USB 驱动、数据线与 RSA 授权。
- 400 invalid bounds format / 404 xpath not found
  - 确认 bounds 形如 `[x1,y1][x2,y2]`；或调小 XPath 复杂度（ElementTree 支持有限），最好用 `.//node[@resource-id='...']` 这类表达式。
- Mixed Content / 浏览器拦截
  - 当页面为 HTTPS 而请求本地 HTTP 接口时，浏览器可能提示混合内容。现代浏览器一般对 `http://127.0.0.1` 网段放宽；如被阻止，可在站点设置里允许“不安全内容”，或使用控制台手动注入脚本。
- JSON/图像序号错乱
  - 桥接按 `collected_data.json` 当前长度作为下一个序号；每次启动若带 `--reset-json` 会清空并从 0 开始。
- 无红框
  - 可能未找到节点或节点缺少 bounds 属性；此时 boxed 与 raw 相同。
- 日志位置
  - 桥接日志：`adbgetevent/UI_Automated_acquisition/bridge_server.log`（设置 `BRIDGE_DEBUG=1` 可在响应里带堆栈）

## 六、文件位置索引
- 核心监听：`adbgetevent/run_collector_adb.py`
- 桥接服务：`adbgetevent/bridge_server.py`
- 自动发送脚本（服务端托管）：`GET /autosend.js`
- 浏览器悬浮面板脚本样例：`adbgetevent/../Auto脚本.txt`
- 采集输出根目录：`adbgetevent/UI_Automated_acquisition/`

## 许可证
仅用于内部数据采集与测试用途。