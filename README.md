# 微信聊天档案

Windows 本机网页工具，用于维护微信 4.x 聊天记录并导出 TXT、JSON、ZIP。
服务监听 `127.0.0.1`；密钥、数据库、语音识别和导出均在本机处理。

## 安装

需要 Windows 和 Python 3.10 或更高版本。以下命令均在项目根目录执行；
`python` 表示你准备用来运行本项目的解释器，也可以替换成其完整路径。

先安装网页、数据库消息解析和导出所需的依赖：

```powershell
python -m pip install -r requirements.txt
python run.py web --open-browser
```

这一步即可使用数据准备、聊天维护和导出。SQLite、文件处理和 Windows 密钥工具
使用 Python 标准库及 Windows 系统接口，无需另装 SQLite、加密库或数据库服务。

**语音转写为可选功能。** 不使用时无需安装 `requirements-voice.txt`。
使用时先确定运行识别的 Python 解释器，在该环境安装：

```powershell
python -m pip install -r requirements-voice.txt
python local_asr.py --probe
```

语音清单提供 Whisper 识别、SILK 解码和 FFmpeg 音频转换。
Whisper 依赖 PyTorch；如需 GPU 加速，应先按显卡和 CUDA 条件准备兼容的 PyTorch。
已有可用的 PyTorch 环境可直接复用，不需要另建环境。
还须预先准备 Whisper 模型 `.pt` 文件，放入当前识别用户的
`%USERPROFILE%\.cache\whisper\`。`--probe` 输出工具是否可用及发现的模型；
安装 Python 包不会自动准备模型，应用运行时也不会下载模型。

**当前启动配置：** 双击入口中的解释器路径位于 `启动微信聊天档案.cmd`；
语音解释器路径位于 `voice_service.py` 的 `PYTHON`。
两处目前分别指向本机的 appdev 和 pytorch 环境。其他电脑使用时需改为实际解释器路径；
两者可以使用同一环境，也可以分开。依赖必须安装到对应解释器中。

## 启动与使用

双击 **启动微信聊天档案.cmd**。入口使用本机已有环境：
`D:\efficiency\anaconda3\envs\appdev\python.exe`，服务就绪后自动打开
`http://127.0.0.1:8765`。关闭服务窗口或按 Ctrl+C 停止服务。

1. 网页输入微信 `db_storage` 根目录，点击检查。也可使用已有解密目录；
   根目录应包含 `contact/contact.db` 和 `message/message_*.db`。
2. 填写用于区分档案的账号标签，勾选账号归属确认，保存目录。
3. 加密库：保持微信登录，点击提取密钥。也可在“使用已有密钥文件”中
   保存本账号的密钥 JSON 路径。然后点击解密准备数据，**可保持微信打开**。
   实际写入或 checkpoint 占用锁时会短暂重试；持续占用时再退出微信后重试。
   旧档案仍可导出。
4. 输入公开微信号或内部 username 查找身份。实际联系人表的 `alias`
   与 `username` 是不同字段，网页展示映射结果供确认。没有 alias 映射时，
   从微信账号目录核对内部 wxid，输入后查找；只有数据库中存在的身份才可确认。
5. 确认自己的内部身份和显示名，点击更新聊天数据。后续更新也由此按钮手动触发。
6. 搜索、按类型和聊天起止时间筛选，按最近消息或数量排序，多选聊天。
   群聊显示“已收录发送者”人数，鼠标停留可查看名单；该名单来自历史消息。
7. 可选择聊天并点击转写。预览显示最近 5 条消息，转写状态和失败原因随语音显示。
   单选 TXT/JSON 下载一个文件；多选 TXT/JSON 下载对应格式的 ZIP；
   “导出 ZIP”同时包含每个聊天的 TXT 和 JSON。

任务显示阶段、进度、结果和下一步。维护任务串行执行，执行期间可查询已发布档案
和导出；切换账号、身份和工具配置需要等待当前任务结束。没有定时后台更新。

## 持久维护

`local_data/accounts.json` 保存账号与目录绑定。每个目录对应一个独立账号档案，
不同账号的身份、消息、metadata、任务状态和转写结果分别保存在
`local_data/<随机账号ID>/`。切换到已保存目录需再次确认归属；同一目录不能改绑到
另一个账号标签。已有密钥、原解密目录和导出文件不会被自动删除。

- 消息标识：会话 username + 非零 server_id；没有 server_id 时使用会话 +
  来源分片 + local_id + create_time。不会假定 local_id 跨分片唯一。
  同一位置从本地消息升级为 server_id 时合并；相同 server_id 的重复分片记录去重，
  冲突内容会使本次导入失败，避免任意覆盖。
- 更新扫描源消息进行比对，只插入新增或更新变化记录；已有档案不会全量重建。
  保留源库消失的历史。可识别的类型、状态和内容变化（包括源库实际反映的撤回）会更新。
- metadata 包含稳定会话标识、名称、类型、起止时间、总数、最后消息类型与预览，
  以及群聊已收录发送者列表。列表读取这些已维护字段，不扫描源消息。
- 消息、metadata、上次成功更新时间和解密代次指针在同一个 SQLite 事务中提交。
  导入失败或进程中断会回滚；重启显示中断任务，可重新执行。
- 解密结果写入新代次；源主库与 WAL 的摘要用于跳过未变化数据库的 AES 解密。
  摘要计算替代更昂贵的认证与解密。每次仍需稳定快照和源消息比对，更新并非仅读新增行。
  新代次复制未变化的明文库，旧代次保留，因此需要足够磁盘空间。

## 身份依据

每个分片独立识别：filehelper 中唯一的非系统消息发送者 → 用户确认的
username 在该分片 `Name2Id.user_name` 对应 rowid → 出现编号 4 则使用 4，
否则使用 1。JSON 消息记录保存 `identity_basis`。群发送者使用当前分片的
Name2Id 映射，未知成员保留分片和编号，不合并到群名称。

## 本地语音

识别 worker 使用 `voice_service.py` 中配置的 Python 解释器。
网页检查 Whisper、模型缓存、SILK 解码包和 FFmpeg；发现 `small.pt` 时默认选用，
也可在网页选择其他已缓存模型。
worker 只加载存在的 `.pt` 文件，不下载模型、不调用云服务。

媒体关联经 `message/media_*.db.Name2Id` 查找会话 rowid，再以会话编号、local_id、
svr_id、create_time 精确查询 `VoiceInfo.voice_data`。SILK 通过 `pilk` 解码为 PCM/WAV，
再本地识别。成功文本进入预览、TXT/JSON，原语音类型、媒体关联与状态仍保留。
已成功且媒体字节未变化的结果复用；更新时发现媒体变化则使旧转写失效。
媒体被清理后仍保留已有成功结果。未缓存媒体、工具缺失、解码或识别失败分别记录原因，
不影响其他消息。没有语音播放或媒体文件打包。

## 加密 WAL 与验证边界

实现按照 [SQLite WAL 格式](https://sqlite.org/fileformat.html#the_write_ahead_log) 和
[SQLCipher 的 WAL 写入代码](https://github.com/sqlcipher/sqlcipher/blob/master/src/wal.c)
处理 4096 字节页：校验头、salt 和累积帧 checksum；只取最后一个有效提交及其之前的帧；
按数据库真实页号认证 SHA-512 HMAC，并使用现有 AES-256-CBC 页面格式解密，
将最新提交页写入明文主库，按提交页数截断。输出没有独立明文 WAL。
发布前执行 SQLite quick_check。
同步仅包含联系人库、数字编号消息分片（`message_0.db` 等）和语音库；
`message_fts.db`、`message_resource.db` 等辅助库不参与聊天档案导入，避免依赖微信专用的全文索引扩展。

Windows 对离线文件取得禁止写入的只读句柄；存在正常打开的微信数据库连接时，
使用 SQLite Windows VFS 的主库共享锁，以及 WAL-index 的写入、checkpoint、恢复锁
复制稳定快照。打开着写权限句柄本身不会导致失败；实际事务占用时短暂重试。
快照锁释放后微信可以继续写入。权限不足、文件变化与锁忙分别报告。
原始加密 WAL 不会直接放到明文库旁使用。

**限制与待验证部分：**

- 已读取本机真实表结构及原始 WAL 头，确认 4096 页与标准 WAL 头；
  本次未执行真实进程读密钥、真实数据解密或真实语音识别。
- 加密 WAL 已通过 AES 加密合成数据库测试，**尚未通过真实微信加密 WAL 端到端验证**。
  所需最小证据是一组稳定、匹配且含有效提交的原始 `.db` / `.db-wal` 和本机已有密钥，
  再核对一个已知只在 WAL 中的消息是否进入结果。由用户在网页触发处理；无需上传聊天或密钥。
  正常退出微信可能已 checkpoint 掉 WAL，此时不能用“解密成功”代替非空 WAL 验证。
- 未安装 pilk 时，微信 SILK 会显示工具缺失。
  使用本地 Whisper 的代码链路已接通，成功/复用测试使用合成媒体与模拟识别器，
  未据此声称真实微信语音识别已通过。
- 不推断源库没有记录的撤回，也不将系统撤回提示任意关联到另一条消息。
- 本版本按已查证的微信 4.x 结构读取；不支持微信 3.x。

## 开发与验证

依赖安装见上文。测试使用标准库 unittest，无需另装测试框架。

```powershell
python run.py web --open-browser
python -m unittest discover -s tests -v
```

保留开发 CLI：`run.py list --db <明文目录>`、
`run.py export --db <明文目录> --contact <关键词>` 或 `--all`，可指定 `--out`。
CLI 导出直接读取源明文库；网页导出读取维护后的档案与转写结果。
`run.py web --db` 仅修改网页目录建议，账号仍需在网页确认。
`wcdb_key_tool_windows.py decrypt --db-dir <源目录> --keys <JSON> --output <新目录>`
也使用页面认证和加密 WAL 合并；每个库独立发布，完整账号事务维护由网页完成。

合成测试覆盖身份三条路径、多分片标识与升级、变化更新、历史保留、回滚、重启、
账号隔离、metadata、筛选与导出、本地语音成功/复用/缺失/失败、WAL提交边界、
不完整尾部、校验失败、页面认证、稳定快照及未变化数据库复用。
浏览器验收可运行 `tests/ui_fixture_server.py`，它只生成临时合成数据，监听 8767。

密钥工具源自 [wcdb-key-tool](https://github.com/TANGandXUE/wcdb-key-tool)。
