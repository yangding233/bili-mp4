# Bili MP4

中文 Windows 桌面软件：粘贴 B 站视频链接，选择分 P 和实际可用格式，下载后无损封装成带声音的 MP4。每个 P 独立输出。

当前版本为 **0.1.0 首版开发版**。源码、自动化测试和 Windows 构建工作流一同提供；当前工作环境未执行 Python、Qt 和 FFmpeg，暂无真实 B 站视频测试结果，因此这里不宣称本机测试、真实下载或干净 Windows 验收已经通过。实际 CI 结果以仓库的 [Actions](https://github.com/yangding233/bili-mp4/actions) 为准，人工验收见 [验收清单](docs/acceptance.md)。

## 首版功能

- 输入 BV 号或普通视频页面链接；链接中的 `p=` 用于默认选中对应分 P。
- 分 P 列表、全选、多选，以及每个 P 的实际可用清晰度、帧率和编码。
- 默认兼容性优先：优先 H.264/AAC，并在同编码内选择最高分辨率；也可指定清晰度。指定档位不可用时提示，不自动降级。
- 持久化下载队列、分阶段进度、下载速度和预计剩余时间。
- 暂停、继续、取消、失败重试和重启恢复；无法确认资源一致性时安全重新下载。
- 视频、音频分别下载，FFmpeg `-c copy` 封装，检查音视频流、分辨率和时长后提交成品。
- 中文路径、文件名清理、任务独立临时文件、不覆盖已有成品。
- 打开输出目录、导出经过脱敏的诊断日志。

只支持匿名可访问的普通点播视频，且首版仅下载已识别编码、使用直接 HTTP 地址并可直接无损封装为 MP4 的格式；分片清单、未知编码或需要转码的格式暂不支持。清晰度由 B 站当时返回的格式和访问权限决定，不能保证 1080p、4K 或某种编码一定可用。首版不支持账号或 Cookie 导入、DRM、付费权限绕过、直播、跨 P 拼接、转码、字幕、弹幕、AI 视频总结。

## 使用步骤

1. 粘贴视频链接或 BV 号，点击“解析视频”。
2. 勾选需要下载的 P，点击“查看所选格式”，为各 P 选择实际可用格式。也可以把同一清晰度要求应用到勾选的 P。
3. 选择保存目录，点击“加入队列并下载”。
4. 队列逐个处理任务。选中任务后可暂停、继续、取消或重试。
5. 阶段显示“完成”后打开输出目录，播放生成的 MP4。

暂停下载保留进度；合并暂停或取消后，需要重新合并，已经通过检查的输入流可复用。取消默认保留任务临时文件，首版没有自动删除取消任务缓存的界面操作。软件重启后，未完成任务等待用户继续，不自动发起网络下载。

无损封装不会重新编码。MP4 是容器，实际播放器兼容性仍取决于所选编码；H.264/AAC 通常更便于播放。[FFmpeg streamcopy 说明](https://ffmpeg.org/ffmpeg.html#Streamcopy)

## 获取 Windows 便携包

本项目目前通过 GitHub Actions 构建测试包，**不预先承诺已有正式 Release**。

打开 [Actions](https://github.com/yangding233/bili-mp4/actions)，选择成功的“Windows checks and portable build”，下载 `BiliMP4-windows-x64` 产物。下载 Actions 产物通常需要登录 GitHub。解开产物中的 ZIP，再完整解压 `BiliMP4-0.1.0-windows-x64.zip`，运行 `BiliMP4/BiliMP4.exe`。

不要只复制 EXE；`_internal`、内置工具和许可文件必须一起保留。便携包由构建脚本纳入 Python、Qt、yt-dlp、FFmpeg/ffprobe，目标是不要求用户自行安装运行依赖。Windows 10/11 x64 的完整人工验收仍以验收记录为准。

## 从源码运行

需要 Python 3.11 x64，以及同时含有 `ffmpeg.exe` 和 `ffprobe.exe` 的工具目录。以下命令在项目根目录的 PowerShell 执行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
$ffmpegBin = .\scripts\fetch_ffmpeg.ps1
.\launch.ps1 -FfmpegDirectory $ffmpegBin
```

`fetch_ffmpeg.ps1` 会从发布方下载固定的 Gyan FFmpeg 8.1.2 essentials ZIP，校验固定 SHA256 后解压到项目的 `tools/`。它不会安装到系统或修改永久 PATH。也可使用自己已有的工具目录，在界面选择工具位置；软件不自动下载或安装 FFmpeg。

已有 Python 时也可执行 `.\launch.ps1 -Setup` 创建环境、安装项目依赖并启动。运行环境已就绪后，启动命令为 `.\launch.ps1` 或 `.\.venv\Scripts\python.exe -m bili_mp4`。

任务数据库和设置默认位于 `%LOCALAPPDATA%\BiliMP4`；开发检查可通过 `--data-dir` 指定独立目录。视频存放在界面选择的输出目录。不要提交数据库、临时流、媒体文件、授权信息或日志到公开仓库；这些路径已加入忽略规则。

## 测试与构建

```powershell
$ffmpegBin = .\scripts\fetch_ffmpeg.ps1
$env:BILI_MP4_FFMPEG_DIR = $ffmpegBin
$env:PATH = "$ffmpegBin;$env:PATH"
.\.venv\Scripts\python.exe -m compileall -q src tests scripts
.\.venv\Scripts\python.exe -m pytest
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m bili_mp4 --smoke-test --data-dir .\build\smoke-data
.\scripts\build_windows.ps1 -FfmpegDirectory $ffmpegBin
```

自动化测试使用解析 mock、可控下载器和真实 localhost HTTP 服务，CI 不下载 B 站视频。媒体封装、完整解码和真实播放按验收清单另行验证。Windows 工作流执行语法检查、pytest、源码界面启动检查、PyInstaller 打包，以及打包后的启动检查。界面启动检查不能代替真实下载、音画同步或 Windows 10 人工验收。

构建结果位于 `dist/`，同时生成 ZIP 的 SHA256 文件。构建脚本不会删除原项目或自动清理已有构建；目标 ZIP 已存在时会停止，避免覆盖。

## 技术结构

`src/bili_mp4/` 使用独立模块组织：

| 模块 | 职责 |
| --- | --- |
| `domain.py` | 可序列化任务、分 P 和格式模型 |
| `resolver.py` | 页面链接、分 P 解析、yt-dlp 格式选择 |
| `network.py` | HTTP Range、资源校验、下载与有限重试 |
| `store.py` | SQLite 任务持久化和恢复 |
| `mux.py` | 查找工具、ffprobe 检查、FFmpeg 无损封装 |
| `engine.py` | 单任务执行、队列调度、暂停取消、成品提交 |
| `ui.py` | 中文 PySide6 界面和后台事件展示 |

固定直接依赖版本：PySide6 6.11.2、yt-dlp 2026.8.19、PyInstaller 6.22.3、pytest 9.1.1。构建记录保存当次完整依赖版本；上游解析规则变化时需要更新依赖并重新验收。

续传依赖服务端范围响应及可靠的资源校验信息，不承诺每次中断都能原位继续。只有 CID/格式身份、强 ETag 和总长度一致时才继续追加；如果链接刷新后无法确认同一资源，则重新下载。服务端 ETag 是资源一致性标识，不是远端内容的加密真实性证明。已完成的本地流使用 SHA256 检查缓存完整性，未完成的 .part 暂无独立前缀校验。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 视频解析失败、删除或不可访问 | 检查普通视频链接和当前匿名访问权限；持续失败时查看诊断信息 |
| 缺少目标清晰度 | 查看各 P 实际格式，手动选择可用清晰度 |
| 下载地址失效 | 软件有限次数重新解析；持续失败时修复网络或稍后手动重试 |
| 断网、超时、限流 | 软件有限重试；暂停或稍后继续，避免反复同时发起任务 |
| 提示缺少 FFmpeg | 使用完整便携包，或选择含 FFmpeg/ffprobe 的目录 |
| 目录不可写、空间不足、文件被占用 | 更换可写目录、释放空间或关闭占用文件的程序后重试 |
| 合并失败 | 保留输入流；重试合并，或重新选择兼容格式 |
| 合并后声音或同步异常 | 不把“存在两路流”当作完整播放保证；提供脱敏日志及现象供排查 |

请只下载你有权访问和保存的内容。软件不设计绕过 DRM、付费权限或访问控制的功能。

## 许可证与第三方组件

本项目自己编写的代码采用 [MIT](LICENSE)。第三方组件保留其各自许可证，MIT 不替代 Qt/PySide6、Python、yt-dlp 或 FFmpeg 的许可。

构建脚本保留第三方许可文本、FFmpeg 版本/配置及源码获取路径。Gyan 静态 FFmpeg 构建采用 GPLv3；正式分发便携二进制前，需要完成对应源码及构建材料的核对和提供。具体记录见 [第三方说明](THIRD_PARTY_NOTICES.md)。
