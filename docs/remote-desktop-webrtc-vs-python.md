# 远程桌面：WebRTC 与纯 Python 像素流的差距与技术账单

> 结论先行：**两者差的不是"能不能把画面传过去"，而是"网络变差时还能不能看"。**
> 纯 Python 方案在局域网、少并发、看界面点按钮的场景里，用 1/5 的成本做到 WebRTC 80% 的体验；
> 一旦跨公网、丢包、上行受限、要 30fps，WebRTC 的那套"传输大脑"就是刚需，自己补等于重写半个 libwebrtc。
>
> 本文除标注"预估"外，数字均来自本机实测（Windows 11 25H2 / 1920×1080 / 8 核），复现脚本见文末。
>
> **原型已验证**：纯 Python 像素流（DXGI + 分块 + 按需采集）实测典型场景 **CPU 7% 单核 / 0.33 Mbps / exe +30 MB**，
> 已落在 RDP 的区间内；与 WebRTC 的差距集中在公网弱网与帧间压缩，详见第 8 节。

---

## 1. 先把"纯 Python"和"WebRTC"拆开

这两者不是同一个层面的东西，直接对比会失真。实际是四种形态：

| 形态 | 服务端 | 客户端 | 本质 |
|---|---|---|---|
| **A. MJPEG** | Python 抓屏 → JPEG → `multipart/x-mixed-replace` | `<img src>` | 最古老，零 JS |
| **B. 帧流** | Python 抓屏 → JPEG/WebP → SSE/WebSocket | `<canvas>` + `drawImage` | 可控，可加脏矩形 |
| **C. PyAV 编码 + MSE/WebCodecs** | Python + FFmpeg → H.264 fMP4 | `<video>` 硬解 | "半 WebRTC"，无拥塞控制 |
| **D. 真 WebRTC** | aiortc（Python）或 Chromium（Electron） | `RTCPeerConnection` | 完整 RTP/RTCP 栈 |

只有 D 是 WebRTC。A/B/C 都属于"纯 Python 像素流"家族——C 已经用上了硬件解码，但传输层仍然是 HTTP/TCP，仍然没有丢包恢复和码率自适应。

**关键认知：WebRTC 的价值 80% 在传输层，不在编解码。** 它免费给你的东西是：

- SRTP over UDP，包级 MTU 切分（1200B），一个包丢了只影响那一小块；
- NACK / RTX 重传、ULPFEC 前向纠错；
- 抖动缓冲（jitter buffer），把乱序和抖动吃掉；
- 发送端 pacing，避免突发把链路打爆；
- GCC / transport-cc 带宽估计 + 动态码率/分辨率；
- ICE/STUN/TURN 穿透，以及 DTLS 握手加密；
- 浏览器侧硬件解码、AEC 回声消除、音量自适应。

纯 Python 方案默认一样都没有。

**但要注意：用 aiortc 不等于拿到完整的 libwebrtc。** 翻了 1.16.0 的源码，实际能力是：

| 能力 | libwebrtc（浏览器/Electron） | aiortc 1.16 |
|---|---|---|
| NACK 重传 | ✅ | ✅（`RTCRtpSender._retransmit`） |
| 关键帧请求 PLI/FIR | ✅ | ✅ |
| 抖动缓冲 | ✅ | ✅（`jitterbuffer.py`） |
| 带宽估计 + 码率自适应 | ✅ GCC + transport-cc | ⚠️ 仅 REMB + `rate.py` 的速率控制，无 transport-cc |
| 发送端 pacing | ✅ | ❌ |
| ULPFEC / FlexFEC | ✅ | ❌ |
| 硬件编解码 | ✅ NVENC/QuickSync | ❌ 默认 libx264/libvpx 软编（要自己接 NVENC 得改代码） |

一句话：**aiortc 补上了"能通话"的下限，补不上"弱网体验"的上限。** 差距主要在 pacing 和 FEC——没有 pacer，突发流量在窄上行链路上照样会顶爆队列。

---

## 2. 差距到底有多大：四个维度

### 2.1 传输语义：TCP 的队头阻塞是硬伤

| 场景 | 纯 Python（HTTP/TCP） | WebRTC（UDP+SRTP） |
|---|---|---|
| 局域网 0 丢包 | 无差别 | 无差别 |
| 1% 丢包 + 30ms 抖动 | 单个丢包卡住整帧，画面"顿一下" | NACK 重传，用户几乎无感 |
| 5% 丢包 | 帧间隔抖动到几百 ms，操作延迟爆炸 | 降码率 + FEC，仍可用 |
| 上行 1Mbps | 要么糊要么卡，只能手工调质量 | 自动降到 480p/5fps 继续跑 |
| 4G/酒店 WiFi | 基本不可用 | 可用 |

TCP 的机制决定了：**丢一个包，后面所有已到达的包都得等它重传**。对"实时画面"这种迟到即无用的数据，这是结构性缺陷，不是调参能解决的。

### 2.2 编码：JPEG 全帧 vs 帧间预测

桌面画面的特点是**大量静止区域**。这决定了两条路线的带宽差距：

- JPEG 是帧内编码，每一帧都独立压缩，静止画面也要完整再传一遍；
- H.264/VP8 是帧间预测，静止画面只传几个字节的 skip 宏块，编码器输出接近 0。

**实验一：同一张界面图（RemoteFM 进程列表，原生 1440×900），只换编码器。**

| 编码 | 参数 | 单帧大小 | 编码耗时 | 码率 |
|---|---|---|---|---|
| JPEG | q85 | 135.1 KB | 6.5 ms | 11.1 Mbps @10fps |
| JPEG | q75 | 115.4 KB | 7.7 ms | 9.5 Mbps @10fps |
| JPEG | q70 | 107.8 KB | 8.1 ms | 8.8 Mbps @10fps |
| **H.264 关键帧（I）** | ultrafast / crf30 | **70.0 KB** | 4.6 ms | — |
| **H.264 后续帧（P，静止）** | 同上 | **11 B ~ 18 KB** | ~1 ms | **1.0 Mbps @30fps** |
| **H.264 后续帧（P，滚动）** | 同上 | — | ~1 ms | **1.1 Mbps @30fps** |

30 帧实测数据：静止画面合计 121.5 KB，滚动合计 134.6 KB（滚动是纯位移，运动补偿几乎全吃掉）。

**实验二：1920×1080 真实桌面抓屏（Pillow `ImageGrab` 原图），JPEG 单帧约 157 KB（q75），编码 7.5 ms，10fps 就是 12.9 Mbps。**

两个数字值得记住：

1. **同样的像素，H.264 的 I 帧比 JPEG 小 40%**（70.0 KB vs 115.4 KB @q75）——帧内编码的熵编码和预测也比 JPEG 强。
2. **同一段内容跑 30fps：H.264 实测 1.0 Mbps，JPEG 按 115.4 KB/帧推算要 28.4 Mbps，差 28 倍。** 静止画面的 P 帧可以小到 11 字节；x264 会周期性补内部刷新块，单帧会跳到十几 KB，但整段平均仍是 JPEG 的零头。

也就是说：**纯 JPEG 全帧推送，1080p/10fps 要 12~19 Mbps**。公网家宽上行通常 20~50 Mbps，一个人就吃掉大半。

必须叠加**脏矩形（dirty rect）**才现实：只重传变化的瓦片（比如 64×64 分块）。打字场景通常只有 2~5% 面积变化，带宽可降到 **0.3~1.5 Mbps**；播放视频时退化为全帧，再靠降帧率兜底。

> 这一条是纯 Python 方案能不能用的分水岭。没有脏矩形，它只能当"截图预览"；有了脏矩形，局域网内体验接近早期 VNC。

### 2.3 采集：BitBlt 36ms 是天花板

本机实测（Pillow `ImageGrab`，底层 GDI BitBlt）：

```text
抓屏一次               36.0 ms（最快 31.0 ms）
抓屏 + 转 numpy        51.8 ms
1080p → 720p 双线性    20~31 ms（PIL resize）
1080p → 960×540 盒式   3.2 ms（PIL reduce(2)）
cv2 JPEG 编码（720p）  4.0 ms
```

抓屏 36 ms 意味着**单线程上限约 27fps，实际可用 10~15fps**。想上 30fps 必须换采集接口：

| 采集方式 | 延迟 | 脏矩形 | 实现成本 | 备注 |
|---|---|---|---|---|
| GDI BitBlt | 30~40 ms | 无（要自己比对） | 0.5 人日 | 兼容性最好，锁屏黑屏 |
| DXGI Desktop Duplication | 1~5 ms | **原生提供** | 3~5 人日 | Win8+，独占全屏/GPU 场景强 |
| Windows Graphics Capture | 3~8 ms | 部分 | 5~8 人日 | Win10 1903+，可单窗口采集，有黄色边框 |
| PrintWindow | 10~50 ms | 无 | 1 人日 | 可抓被遮挡窗口，GPU 应用常黑屏 |

**DXGI 是纯 Python 路线唯一值得投入的性能改造**：它同时解决"快"和"知道哪里变了"两个问题，是脏矩形的天然数据源。

### 2.4 端到端延迟构成

局域网 1080p 实测/推算：

```text
纯 Python（JPEG + SSE）：
  抓屏 36 + 缩放 3 + 编码 7.5 + 传输 2 + 浏览器解码 8 ≈ 56 ms
  加帧间隔（10fps）平均 50 ms → 体感 110~160 ms

WebRTC（aiortc + x264 ultrafast，编码为实测 3 ms）：
  采集 5 + 编码 3 + 网络 3 + 抖动缓冲 25 + 解码 4 ≈ 40 ms
  加帧间隔（30fps）平均 17 ms → 体感 60~90 ms

WebRTC（浏览器 + NVENC 硬编）：
  采集 3 + 编码 3 + 网络 3 + 抖动缓冲 15 + 硬解 3 ≈ 27 ms
  体感 40~60 ms
```

同一台机器、同一个网络，**WebRTC 的体感延迟约为纯 Python 的 1/2，帧率是 2~3 倍**。但这个差距在"看文件、点按钮"的运维场景里，用户往往感知不到——这是纯 Python 方案能成立的现实基础。

---

## 3. 打包体积账单（PyInstaller 单文件）

RemoteFM 当前基线：

```text
dist\RemoteFM.exe        18,854,518 字节（18.0 MB）
依赖                      Flask / requests / psutil / stdlib
```

WebRTC 路线的增量依赖（PyPI 实测 wheel 体积）：

| 包 | 版本 | wheel 体积 | 作用 |
|---|---|---|---|
| aiortc | 1.16.0 | 0.1 MB | 纯 Python 的 RTCPeerConnection |
| av (PyAV) | 19.0.2 | **26.8 MB** | 绑定 FFmpeg，编解码 + RTP 打包 |
| pylibsrtp | 1.0.0 | 1.5 MB | SRTP 加解密（C 库） |
| cryptography | 50.0.1 | 10.5 MB | DTLS 证书与密钥交换 |
| pyopenssl + aioice + pyee + crc32c | — | ~1 MB | 证书、ICE、事件、校验 |
| **合计** | | **wheel 约 40 MB / 解压后 89 MB（实测）** | |

这不是估算，本机实打实构建了两个 PyInstaller 单文件（同一版本 PyInstaller、同样 `--onefile`、未装 UPX）：

| 构建内容 | exe 体积 | 构建耗时 | 启动耗时（同机同法） |
|---|---|---|---|
| `print('ok')` 空壳 | 8.3 MB | 28 s | 约 6 s |
| 空壳 + `import aiortc, av` | **98.1 MB** | 201 s | **29~43 s** |

**增量 +89.8 MB**，构成很直观：

| 来源 | 体积 | 说明 |
|---|---|---|
| FFmpeg（`av.libs` 23 个 DLL） | 64.1 MB | avcodec / avformat / swscale / x264… |
| numpy | 28 MB | av 的可选依赖被 PyInstaller 一起卷进来 |
| cryptography + OpenSSL | ~10 MB | DTLS 证书与密钥交换 |
| pylibsrtp / aioice / aiortc 等 | ~2 MB | |

推算到 RemoteFM：现有 18.8 MB（比空壳多 10.5 MB 的应用依赖）+ 89.8 MB ≈ **105~110 MB**。把 numpy 显式排除（aiortc 的编码路径不走 ndarray）大约能回到 **80 MB 左右**，仍然是现在的 4 倍。

启动变慢同样是实测：同一台机器，8.3 MB 的壳约 6 秒，98 MB 的壳 29~43 秒。原因是 onefile 每次启动都要把上百个 DLL 解压到 `%TEMP%` 再加载——这台机器 C 盘只剩 15 GB、I/O 偏慢，绝对值会比新机器大，但**倍数关系成立**：体积涨 12 倍，启动涨 5~7 倍。

其余代价：

- **杀软误报率上升**：PyInstaller + FFmpeg DLL 是误报重灾区（本机实测时 Defender 已关闭，所以上面的 30 秒不是杀软造成的）；
- **构建时间**：28 秒 → 201 秒，`--clean` 全量构建还要更久；
- **每次发版都要重新分发热更新包**：100 MB 的 exe 走客户现场带宽，升级体验明显变差。

纯 Python 路线的增量：

| 方案 | 增量 | 说明 |
|---|---|---|
| 只用 Pillow 做 JPEG | +5~8 MB | `ImageGrab` + `save(JPEG)`，无 numpy |
| Pillow + numpy 脏矩形 | +15~20 MB | numpy 用于分块比对 |
| 加 OpenCV | +40~60 MB | 不推荐，只为了 resize/imencode 不值 |

**对比：纯 Python 增量 5~20 MB，WebRTC 实测增量约 90 MB（排除 numpy 后 ~70 MB）。差距 4~5 倍。**

> 顺带一个反直觉的结论：**体积的大头不是 aiortc，是它背后的 FFmpeg 和 numpy。** 这也解释了为什么"用 Electron 做 WebRTC"反而划算——smoon 的 285 MB 里 Chromium 已经包含了完整 WebRTC，启用它不需要再加一行依赖。

> 许可提醒：PyAV 官方 wheel 内含的 FFmpeg 构建通常带 libx264（GPL）。RemoteFM 现在随包分发 `THIRD_PARTY_NOTICES.txt` 和 `licenses/`，如果要商用分发，必须确认 GPL 义务，或改用 VP8/OpenH264 规避。

---

## 4. 代码量与工期账单

以"能用"为标准（不是 demo），单人开发估算：

| 模块 | 纯 Python 像素流 | WebRTC（aiortc） | 说明 |
|---|---|---|---|
| 采集 | 0.5 人日 | 0.5 人日 | 两者相同，都用 BitBlt 起步 |
| DXGI 采集 + 脏矩形 | 3~5 人日 | 3~5 人日 | WebRTC 也需要，除非接受全帧 |
| 编码 | 0.5 人日 | 1~2 人日 | av 的 API 不难，难在码率/关键帧策略 |
| 传输 | 0.5 人日（SSE）/ 1 人日（WS） | 5~8 人日 | ICE/DTLS/SDP 协商、重连、ICE restart |
| 前端渲染 | 0.5 人日（canvas） | 1 人日 | `RTCPeerConnection` + 自动播放策略 |
| 输入注入 | 1~2 人日 | 1~2 人日 | SendInput + scancode + 坐标换算 |
| 多屏 / DPI | 1~2 人日 | 1~2 人日 | 两者都躲不掉 |
| 剪贴板 | 1~2 人日 | 1~2 人日 | 文本 / 图片 / 文件 |
| 音频 | 3~5 人日 | 3~5 人日 | WASAPI loopback + Opus；WebRTC 只是省了 AEC |
| 鉴权 / 安全 | 1 人日 | 2~3 人日 | 信令鉴权、TURN 凭证、DTLS 指纹校验 |
| 打包 / 兼容 / 签名 | 1~2 人日 | 3~4 人日 | 体积、误报、启动时间 |
| 测试矩阵 | 3~5 人日 | 5~8 人日 | 弱网、多分辨率、多浏览器 |
| **合计** | **12~20 人日** | **25~40 人日** | |

**WebRTC 的工期是纯 Python 的 2 倍左右**，多出来的部分几乎全在"协商与穿透"——而这部分恰恰是最难调试、最容易在客户现场翻车的。

再叠加运维：

| 项目 | 纯 Python | WebRTC |
|---|---|---|
| 公网可用性 | 需要端口映射或反向代理 | 需要 STUN（免费）+ TURN（自建/付费） |
| TURN 带宽成本 | 无 | 2 Mbps × 1 小时 ≈ 0.9 GB/会话 |
| 证书 | 现有自签/正式证书即可 | WSS + TURN TLS 都要证书 |
| 排障难度 | 看 HTTP 日志 | 要看 ICE candidate、DTLS 握手、RTP 统计 |

TURN 的成本容易被低估：**云服务器 1 TB/月流量，按 2 Mbps 算只够 110 个会话小时**。如果产品是"给客户装在自己机器上、内网使用"，这笔钱是 0；如果要做公网中转，就是持续支出。

---

## 5. 真正容易被忽略的六笔账

1. **锁屏 / 登录界面 / UAC 安全桌面抓不到。**
   这不是技术选型能解决的：Windows 的 Winlogon 桌面和提升权限的 UAC 提示，普通用户进程既不能抓屏也不能注入输入。纯 Python 和 aiortc 一样黑屏。
   要覆盖这些场景只有两条路：把采集做成 SYSTEM 服务，或直接走 RDP（RDP 天然支持登录前画面）。

2. **受保护内容（DRM）会黑屏。**
   Netflix、部分银行客户端、远程桌面自身窗口，在 BitBlt/DXGI 下都是黑的。WGC 稍好，但也不保证。

3. **UIPI：非管理员进程无法向管理员窗口发送输入。**
   用户以管理员身份打开的窗口，你的 `SendInput` 会被静默丢弃。表现为"画面能看，点了没反应"。解决要么整体提权，要么退化到 `PostMessage`（兼容性更差）。

4. **多显示器 + 缩放。**
   125%/150% 缩放下，物理像素、逻辑像素、`SendInput` 的绝对坐标是三套体系，换算错了就是"鼠标偏一格"。这一项在真实客户现场的支持工单里占比很高。

5. **音频不是"送个 PCM"那么简单。**
   WASAPI loopback 采集的是 48kHz 立体声混音，要重采样、要 Opus 编码、要处理静音检测。WebRTC 的优势只在于浏览器侧的 AEC/AGC，采集端仍然要自己写。

6. **维护风险。**
   aiortc 是单人主导的项目，发版节奏跟随维护者，FFmpeg/PyAV 的二进制兼容问题最终要你自己扛。相比之下，浏览器里的 libwebrtc 由 Google 持续维护，安全补丁跟着 Chrome 走——**这也是 smoon 选择 Electron 而不是 Python 做 WebRTC 的根本原因**。

---

## 6. 三条路线的决策矩阵

| 需求 | 纯 Python 像素流 | aiortc WebRTC | RDP 网关 / Guacamole |
|---|---|---|---|
| 局域网运维、看界面点按钮 | ✅ 最优 | 过剩 | 过剩 |
| 公网 / 弱网 / 4G | ❌ | ✅ | ✅ |
| 30fps 以上流畅度 | ❌（10~15fps） | ✅ | ✅ |
| 锁屏 / 登录前画面 | ❌ | ❌ | ✅ |
| 多用户并发会话 | ❌ | ⚠️ 需自己做会话隔离 | ✅（原生） |
| 单文件绿色分发 | ✅（+5~20 MB） | ⚠️（实测 +90 MB，启动 5~7 倍） | ❌（依赖服务） |
| 开发工期 | 12~20 人日 | 25~40 人日 | 8~12 人日（集成） |
| 长期运维成本 | 极低 | 中（TURN/证书） | 中（Windows 授权） |

**一句话选型：**

- 目标是"远程看一眼、点几下" → **纯 Python**，投入产出比高一个数量级；
- 目标是"像本地一样操作"、要过公网 → **WebRTC**，且优先考虑浏览器/Electron 承载，别用 Python 硬扛；
- 目标是"开机前能进、多人同时用" → **别自己写，上 RDP**。

---

## 7. 落到 RemoteFM 的分阶段方案

RemoteFM 的现状决定了它不该一步到位上 WebRTC：单文件 Flask、18 MB exe、自签 HTTPS、SSE 已经在用于终端流、内网为主。

### 阶段一：纯 Python 像素流（原型已验证，落地约 3~5 人日）

原型 `remote_desktop_py.py` 已经跑通，实测数据见第 8 节。最终形态是：

- **采集**：DXGI Desktop Duplication（dxcam），保留原生 BGRA 不做 `cvtColor`；duplication 失效时自动回退 GDI；
- **差分**：1/8 缩略图、单通道 numpy 比较（0.7 ms），按 64px 网格合并成矩形，一帧可发多个矩形；
- **传输**：HTTP chunked 二进制帧（避开 base64 的 +33%），每连接只取最新帧，慢客户端自动丢帧；
- **调度**：静止 0.8 s 降到 5fps；没人观看时完全停采集；收到远端输入立刻恢复；
- **输入**：`POST /input` + `mouse_event` / `keybd_event`（生产建议换 SendInput + scancode，处理 UIPI）。

实测指标（1920×1080 / 10fps / q70）：

```text
典型办公（0.4% 面积变化）  CPU 7% 单核（整机 0.9%） / 0.33 Mbps
四分之一屏动画（23% 面积）  CPU 17% 单核              / 6.3 Mbps
三人同时观看              CPU 12% 单核              / 1.6 Mbps 总出网
无人观看                  CPU 0%                   / 0 Mbps
exe 增量                  +30 MB（numpy + comtypes + dxcam）
```

验收标准：能流畅浏览文件、点菜单、输入文字；拖动窗口允许轻微掉帧；不做音频。

> 一个容易踩的坑：**降采样别用 `Image.resize`**。1080p→720p 双线性 `resize` 要 20~31 ms，`Image.reduce(2)` 只要 3.2 ms；而在 DXGI 路径下用 numpy 切片 `frame[::2, ::2]` 更快。

### 阶段二：帧间压缩（仅在重负载/公网需要时，3~5 人日）

保留阶段一的采集与调度，只把编码层换成 H.264（PyAV，ultrafast/zerolatency）：带宽可再降 3~5 倍、重负载 CPU 从 17% 降到 10~12%，代价是 exe +64 MB 和 GPL 许可确认。公网场景仍建议直接上 WebRTC（阶段三）。

### 阶段三：需要公网/流畅度时再上 WebRTC（5~8 周 / 25~40 人日）

- 若坚持单文件 Python：aiortc + av，接受 exe 100 MB+、冷启动变慢 5~7 倍（并显式排除 numpy 把体积压回 80 MB 左右）；
- 若可以引入第二个进程：让浏览器/Electron 做 PeerConnection，Python 只做信令和文件管理（smoon 已验证这条路）；
- 同时必须准备 TURN（coturn）+ 证书 + 带宽预算。

### 不建议做的事

- ❌ 用 OpenCV 的 `VideoWriter` 做 H.264 推流：延迟不可控、无 B 帧控制、无码率自适应，比 JPEG 更糟；
- ❌ 自己实现 RTP/RTCP：工作量等于重写 libwebrtc 的 1/10，但会踩完 100% 的坑；
- ❌ 用 MJPEG 直接上公网：13~19 Mbps 的全帧流，家宽上行直接打满。

---

## 8. 原型实测：纯 Python 到底能做到什么程度

原型：`remote_desktop_py.py`（stdlib + Pillow + numpy + dxcam，约 520 行）。
环境：8 核 / 1920×1080 / Win11 25H2，10 fps，JPEG q70，单个观看者（除非注明）。

### 8.1 三轮演进

| 版本 | 采集 | 差分/编码 | CPU 范围（单核） | 带宽范围 |
|---|---|---|---|---|
| v1 | GDI BitBlt（45~90 ms） | bbox 包围盒 | 41~65% | 0.2~8.9 Mbps |
| v2 | GDI BitBlt | 64px 分块 + 自适应帧率 + 无人观看暂停 | 33~59% | 0.14~5.4 Mbps |
| **v3** | **DXGI（4~7 ms）** | 分块 + 1/8 numpy 差分 + BGRA 免转换 | **7~17%** | **0.33~6.3 Mbps** |

三处关键优化各自的贡献：

| 优化 | 效果 |
|---|---|
| 分块替代 bbox | 小窗口动画：25.8% 面积 → 8.5%，带宽 5.22 → 2.22 Mbps |
| 无人观看 / 静止降频 | 常驻 41% → 空闲 8% / 无人 0% |
| DXGI + BGRA（免 `cvtColor`） | 抓屏 60~90 ms → 4~7 ms，典型 CPU 41% → 7% |

### 8.2 各场景实测

| 场景 | 变化面积 | CPU（单核） | CPU（整机 8 核） | 带宽 |
|---|---|---|---|---|
| 典型办公（打字/光标） | 0.4% | **7%** | 0.9% | 0.33 Mbps |
| 15 fps 典型 | 0.6% | 13% | 1.6% | 0.74 Mbps |
| 四分之一屏动画 | 23% | **17%** | 2.1% | 6.3 Mbps |
| 三人同时观看（典型） | 0.4% | 12% | 1.5% | 1.6 Mbps 总出网 |
| 无人观看 | — | **0%** | 0% | 0 |

分阶段耗时（v3，动画场景）：抓屏 p50 5~7 ms、差分+分块 0.7~1.6 ms、编码 1~17 ms（随面积）；
内存 RSS 约 90 MB（dxcam 的 staging 纹理 + 帧缓冲）。

**对照**：RDP 空闲 1~3%、活跃 5~10%（单核）；WebRTC 软编 10~15%。**典型场景已经落在 RDP 区间内**，重负载仍差 7 个百分点，根因是 JPEG 没有帧间压缩，不是采集。

### 8.3 为什么不做"只拷脏矩形"

直觉上，用 DXGI 的 `GetFrameDirtyRects` 只把变化区域从 GPU 拷到 CPU，能把 7.9 MB/帧降到接近 0。实测否决了这个方案。

探针（`GetFrameDirtyRects` + `AcquireNextFrame`，本机）结果：

| 指标 | 实测 |
|---|---|
| 真实变化面积 | 1.5%（220×140 窗口在动） |
| 驱动上报脏面积 | **31.3%（中位）**，1 个矩形，坐标为整个前台窗口 |
| 每帧拷贝量 | 7.9 MB → 2.5 MB（约 3 倍） |
| 每帧省时 | 约 1.2 ms（10 fps 约合 12 个百分点） |
| 收益换算 | 典型 7% → 4~5%；重负载 17% → 15% |

原因：**DWM 合成后，脏矩形是"窗口粒度"的**。终端里一个字符闪烁，整个窗口被标记为脏；脏矩形面积取决于"有几个窗口在动"，而不是"动了多少像素"。

结论：**投入 2~3 天 COM 封装 + 驱动差异风险，换 12 个百分点的典型场景收益，且换一台机器可能归零——不划算。** 应用层分块已经把"少编码"这份收益拿到了（6 Mbps → 0.33 Mbps），剩下的拷贝下限是 GPU→CPU 的物理传输，再压也只有 0.55 ms。

> 探针脚本：`probe_dirty_rects.py`。换机器跑一下就知道那台的驱动是否上报细粒度；作为通用能力不能押注在驱动行为上。

### 8.4 对第 7 节的影响

阶段一从"预估 12~20 人日"收敛为"已验证、落地 3~5 人日"，且 CPU 指标从"单核 44%"改为实测 7%。**DXGI 不是可选项，是阶段一的前提**——没有它，典型 CPU 会在 40% 上下，风扇转起来的那一刻产品就输了。

---

## 9. 附：本机实测方法

环境：Windows 11 25H2 (26200.9711)、1920×1080、8 核、Python 3.12、Pillow 12.3、OpenCV 4.11。

抓屏与编码测量（取 15 次中位数）：

```python
from PIL import ImageGrab
import time, statistics

xs = []
for _ in range(15):
    t = time.perf_counter()
    img = ImageGrab.grab(all_screens=False)
    xs.append((time.perf_counter() - t) * 1000)
print('grab median ms =', round(statistics.median(xs), 1))
```

H.264 编码测量（同一张界面图，libx264 ultrafast / crf30 / 单线程）：

```python
import av, numpy as np
from PIL import Image
from fractions import Fraction

img = Image.open('screenshots/processes.png').convert('RGB')
base = np.asarray(img).copy()
h, w, _ = base.shape

c = av.CodecContext.create('libx264', 'w')
c.width, c.height, c.pix_fmt = w, h, 'yuv420p'
c.time_base, c.framerate = Fraction(1, 30), Fraction(30, 1)
c.options = {'preset': 'ultrafast', 'tune': 'zerolatency', 'crf': '30',
             'threads': '1', 'keyint': '250'}
c.open()

sizes = []
for _ in range(30):                      # 30 帧完全相同的画面
    vf = av.VideoFrame.from_ndarray(base, format='rgb24').reformat(format='yuv420p')
    sizes += [p.size for p in c.encode(vf)]
sizes += [p.size for p in c.encode(None)]
print('I 帧', sizes[0], 'B；P 帧范围', min(sizes[1:]), '~', max(sizes[1:]), 'B',
      '；合计', sum(sizes) // 1024, 'KB')
```

实测输出：I 帧约 70 KB，P 帧 11 B ~ 18 KB（x264 会周期性补内部刷新块，属正常现象），30 帧合计 121.5 KB → 30fps 下 1.0 Mbps。

体积与启动测量：

```powershell
python -m pip install --target .\libs aiortc
(Get-ChildItem .\libs -Recurse -File | Measure-Object Length -Sum).Sum / 1MB   # 89 MB（解压后）

python -m PyInstaller --onefile --noconfirm --clean `
  --paths .\libs --distpath .\dist_rtc .\rtc.py     # 98.1 MB，201 秒

Measure-Command { .\dist_rtc\rtc.exe }               # 29~43 秒
```

补充说明：本机未安装 UPX，所以以上是未压缩体积；开启 UPX 通常能再压 20~30%，但会拉长启动时间并提高误报率，是否划算要实测。

---

*文档版本：2026-11-13 · 除特别标注"预估"外，数据均为本机实测；换机器会有 ±30% 浮动，倍数关系不变。*
