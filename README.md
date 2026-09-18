# RemoteFM

**中文** | [English](README_EN.md)

一个 Python 文件，把电脑变成浏览器里的文件管理器：文件浏览、上传下载、在线预览、压缩解压、FTP、网页终端。

## 运行环境

| 项目 | 要求 |
| --- | --- |
| Python | **3.8 及以上**（实测 3.12.10） |
| 操作系统 | Windows / macOS / Linux，无图形界面的服务器同样可用 |
| 依赖 | `Flask`、`requests` 必需；`pyftpdlib` 可选，不装则不启用 FTP |
| 浏览器 | Chrome / Edge / Firefox / Safari 等现代浏览器，手机浏览器也可用 |
| 其他 | 不需要数据库、不需要 Node.js、不需要 Docker |

实测组合：Windows 11 + Python 3.12.10 + Flask 3.1.3 + Werkzeug 3.1.8 + requests 2.34.2 + pyftpdlib 2.2.0。

## 快速开始

```bash
pip install flask requests pyftpdlib
python RemoteFM.py
```

浏览器打开 <http://127.0.0.1:8880>，默认账号 `admin / admin123`。

- 不需要 FTP 功能就不装 `pyftpdlib`，程序会自动跳过。
- 已经装了 [uv](https://docs.astral.sh/uv/) 可以一行运行：`uv run RemoteFM.py`。
- 手机或局域网其他设备访问：`http://本机IP:8880`，首次运行请让防火墙放行。

## 功能

### 文件管理

浏览、搜索、排序、多选，复制 / 移动 / 删除 / 新建文件夹，ZIP 压缩与解压，根目录随时切换。

![文件管理](screenshots/file-manager.png)

### 上传与传输

32MB 分片上传与断点续传，图片 / 视频 / 文本在线预览，也可以填一个 URL 让服务器替你下载。

![上传方式](screenshots/upload.png)

### FTP 直传

与网页共用账号和根目录，复制链接即可粘贴到资源管理器或 FileZilla，适合几十 GB 的大文件。

![FTP](screenshots/ftp.png)

### 网页终端

执行命令并实时回显，支持命令历史、Tab 补全、后台任务，工作目录跟随当前浏览的文件夹。

![终端](screenshots/terminal.png)

### 进程管理

查看进程、CPU 与内存占用、完整命令行，可按名称 / PID / 命令搜索，一键结束进程。

![进程](screenshots/processes.png)

## 配置示例

全部通过环境变量配置，改完即生效，不需要配置文件。

Windows（PowerShell）：

```powershell
$env:SD_ROOT="D:\share"; $env:SD_USER="me"; $env:SD_PASS="换成强密码"; $env:SD_PORT="8899"; python RemoteFM.py
```

Linux / macOS：

```bash
SD_ROOT=/home/me/share SD_USER=me SD_PASS='换成强密码' SD_PORT=8899 SD_FTP=0 python RemoteFM.py
```

只允许本机访问（配合反向代理）：`SD_HOST=127.0.0.1 python RemoteFM.py`

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SD_USER` / `SD_PASS` | `admin` / `admin123` | 登录账号，网页与 FTP 共用 |
| `SD_ROOT` | Windows 为 `D:\`，其他系统为用户主目录 | 被管理的根目录 |
| `SD_HOST` / `SD_PORT` | `0.0.0.0` / `8880` | 监听地址与网页端口 |
| `SD_FTP` / `SD_FTP_PORT` | `1`（启用）/ `2121` | FTP 开关与端口，被动端口 60000-60049 |
| `SD_FTP_PASSIVE_START` | `60000` | 被动端口起始值（连续 50 个） |
| `SD_SECRET` | 每次启动随机 | 会话密钥，固定它可让登录状态在重启后保持 |

## 打包成可执行文件

```bash
python build_exe.py
```

产物在 `dist/`：可执行文件 + `LICENSE` + `THIRD_PARTY_NOTICES.txt` + `licenses/`（各依赖的许可证全文）。
首次运行会自动安装 PyInstaller；图标由脚本内联生成，不需要额外的资源文件。

**运行程序本身只需要 `RemoteFM.py` 一个文件**，`build_exe.py` 只在打包时使用。
发布二进制时请把这几项一起打包：BSD / Apache-2.0 / MPL-2.0 都要求随二进制保留版权与许可声明。

## 安全

**登录密码等于这台电脑的控制权**：它能读写文件、删除文件、执行命令。

- 用之前改掉默认密码，用普通用户运行，并把 `SD_ROOT` 指向需要管理的目录；
- 只在局域网或 VPN 内使用，不要直接暴露到公网；
- 传输默认是明文 HTTP，公网使用请放在 HTTPS 反向代理之后并加上访问控制。

程序不联网上报、没有埋点，唯一的本地写入是系统临时目录中的上传分片；
请只在自己拥有或已获授权的机器上使用，软件按 MIT 协议“原样”提供，不附带任何担保。

## 许可证

[MIT License](LICENSE) © 2026 willdomybest：可自由使用、修改、分发，包括商业用途，保留版权与许可声明即可。本文档与 `screenshots/` 中的截图同样以 MIT 发布。

### 第三方组件

依赖通过 pip 安装，仓库中不包含任何第三方代码；各组件版权归原作者所有，许可证均与 MIT 兼容：

| 组件 | 许可证 |
| --- | --- |
| Flask、Werkzeug、Jinja2、itsdangerous、click、MarkupSafe、idna | BSD-3-Clause |
| blinker、urllib3、charset-normalizer、pyftpdlib | MIT |
| requests | Apache-2.0 |
| certifi | MPL-2.0 |

如果将来把依赖一起打包分发（例如 PyInstaller 单文件版），需要随包附上这些组件的许可文本。

### 贡献

欢迎 Issue 和 PR，提交 PR 即表示你同意自己的贡献以 MIT 协议发布。
