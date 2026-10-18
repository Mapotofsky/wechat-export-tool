# Windows 便携桌面包

支持 Windows 10/11 x64。解压到当前用户可写的目录，双击根目录的 `启动微信聊天档案.cmd`。包内已包含 CPython、桌面窗口和静态前端，使用者无需配置 Python、Conda、Node 或运行 pip。首次使用按照界面选择微信目录、确认账号与本人身份。

程序明确使用 pywebview 6.1 的 EdgeChromium 窗口。需要 .NET Framework 4.6.2 或更新版本，以及 Microsoft Edge WebView2 Runtime；完整语音版还需要 Visual C++ 2015–2022 x64 Runtime。构建时加 `--prerequisites` 会附带微软的 WebView2、.NET 4.8、VC++ 离线安装器。缺少组件时按照启动提示运行 `prerequisites/` 中的安装器；安装可能要求权限或重启。程序不会回退到旧版 IE 内核。

## 目录与更新

| 路径 | 用途 |
|---|---|
| `app/` | Python 程序及 `web/static/` 已构建前端 |
| `runtime/` | 普通 CPython 3.11.9 x64 嵌入式运行时与固定基础依赖 |
| `voice-runtime/` | 完整版的 CPU Whisper、PyTorch、pilk 和语音依赖 |
| `models/small.pt` | 完整版的本地 Whisper small 模型 |
| `tools/ffmpeg.exe` | 完整版音频解码程序 |
| `local_data/` | 首次运行创建的账号配置、SQLite 档案和桌面锁文件 |
| `local_data/logs/` | 桌面启动和运行日志 |
| `licenses/` | CPython、依赖及模型许可、FFmpeg 构建参数与来源 |
| `release-manifest.json` | 依赖版本、程序资源和发布文件清单 |

升级前正常退出程序，把新包的程序目录及启动文件替换到旧目录，保留旧的 `local_data/`。如果把新版本解压到另一个目录，将旧的 `local_data/` 整体复制过去。微信源库、手工选择的密钥和外部模型仍按其原路径访问。`local_data/` 包含本地聊天档案，备份或迁移时自行妥善保存。

程序路径与当前工作目录无关。高级环境变量 `WECHAT_ARCHIVE_HOME` 可明确指定包根目录，`WECHAT_ARCHIVE_DATA_DIR` 可指定持久数据目录，`WECHAT_ARCHIVE_VOICE_PYTHON` 可指定高级语音解释器；日常使用无需设置。基础包不借用构建机的语音 Python，界面会显示语音组件未包含。

## 窗口与本机服务

桌面启动器先占用随机 loopback 端口，启动本进程的 FastAPI 服务，通过 `/api/data-status` 验证就绪后才打开窗口。前端、业务 API 使用同一来源。关闭空闲窗口会停止服务并退出；维护任务或原生保存进行中会阻止关闭，可以最小化继续处理。相同数据目录重复启动会提示已经运行，进程退出后锁自动释放。

原生桥只暴露选择目录、保存导出、打开刚保存的文件和最小化四个动作。保存先请求本机导出 API；全部记录使用 `/api/export`，显式命中范围使用 `/api/export-search`，随后显示保存对话框。建议文件名取自 API 的响应，多会话 TXT/JSON 实际生成 ZIP 时会保留 `.zip` 后缀。仅在文件写入成功后界面报告“已保存”。

## 构建与复现

先按前端开发说明构建 `web/static/`，再在已准备好的 Windows 构建机 Python 环境执行以下命令（示例中的 `python` 指构建解释器）。构建会从官方 Python 下载嵌入式运行时，在独立随包解释器中安装两个 lock 文件固定的直接及传递依赖；不会复制本机 Conda 环境。把已有 Whisper small 模型放在构建目录 `models/small.pt`，或把 `--model` 改为自己的本地模型位置。

```powershell
python scripts/build_portable.py --output 'dist/微信聊天档案 基础版' --prerequisites
python scripts/build_portable.py --output 'dist/微信聊天档案 完整语音版' --voice --model 'models/small.pt' --prerequisites
python scripts/zip_portable.py 'dist/微信聊天档案 基础版' 'dist/微信聊天档案-基础版.zip'
python scripts/zip_portable.py 'dist/微信聊天档案 完整语音版' 'dist/微信聊天档案-完整语音版.zip'
```

`--model` 是构建机已有的模型文件位置，分发后使用随包 `models/small.pt`。构建机首次组装需要联网；完成后的语音识别只读取本地模型和音频。CPU 完整包固定 `torch==2.5.1+cpu`，不要求 GPU。基础版与完整语音版采用相同业务与前端资源。

发布脚本只复制显式 Python 文件清单、`web/app.py`、已构建静态资源、README 和 docs；ZIP 按清单组装，拒绝包含 `local_data/`，不包括源工作目录中的密钥、解密库、聊天档案、导出或测试数据。重新构建同一个输出目录会保留其本地数据；正式分发优先使用清单生成的 ZIP。固定版本保证依赖组合可复现，压缩包时间戳和微软 Evergreen 安装器会随构建日期变化。

## 验证

本轮证据见 [验收记录](acceptance.md)。桌面与语音探测使用合成数据，未读写真实聊天档案：

```powershell
# 实际 EdgeChromium 窗口、HTTP readiness、包内解释器子进程；自动退出
& 'dist/微信聊天档案 基础版/runtime/python.exe' 'dist/微信聊天档案 基础版/app/desktop.py' --smoke-test
# 实际 JavaScript 桥、原生保存与目录对话框、busy 关闭事件、打开合成结果
& 'dist/微信聊天档案 基础版/runtime/python.exe' scripts/probe_native.py --app 'dist/微信聊天档案 基础版/app' --output 'build-cache/native evidence'
# 本机 SAPI 合成中文，SILK 编码后，包内 CPU Whisper small 实际转写
powershell -NoProfile -File scripts/generate_probe_audio.ps1 -Output 'build-cache/probe-speech.wav'
& 'dist/微信聊天档案 完整语音版/voice-runtime/python.exe' scripts/probe_voice.py --app 'dist/微信聊天档案 完整语音版/app' --audio 'build-cache/probe-speech.wav' --output 'build-cache/voice evidence'
```

## 当前限制

已在本机通过包内解释器、中文空格路径、实际 WebView2 窗口和离线语音链路验收；尚未在移除所有开发环境的干净 Windows 虚拟机上运行。随包系统安装器提供离线补齐手段，不等于已经证明所有 Windows 配置均无需系统组件安装。

`licenses/` 保留实际安装依赖附带的许可文件、Whisper 模型 MIT 许可、FFmpeg GPLv3 文本、构建参数和源码来源。FFmpeg 链接多项外部库；公开分发须提供实际二进制及所链接组件对应源码，源码链接记录本身不等于完成全部再分发义务。仓库及所引用密钥工具的代码授权也需在公开发行前明确。本轮交付为本地可运行构建与可复现发布工具。
