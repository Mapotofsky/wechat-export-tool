# 微信聊天记录导出工具（WeChat Chat Export）

一套纯本地运行的微信 PC 版聊天记录导出工具，支持**微信 4.x**（进程名 `Weixin.exe`）。
全流程：提取数据库密钥 → 解密 → 解析 → 按联系人导出 JSON / TXT；也可以在本机网页中多选联系人，下载 ZIP。

> ⚠️ **隐私提示**：数据库、密钥和导出文件都在本机处理，不上传到远程服务器。请仅导出你自己的账号数据，并注意保护对话对方的隐私。密钥提取需要读取微信进程内存。

---

## 目录结构

```
wechat-export-tool/
├── wcdb_key_tool_windows.py   # 密钥提取 + 数据库解密（第三方工具，见下方出处）
├── wechat_export.py           # 核心解析模块（联系人、消息、导出渲染）
├── run.py                     # 统一入口：列出联系人、导出、启动网页
├── requirements.txt           # 命令行和网页共用的依赖
├── README.md
├── tests/                     # 使用合成数据库的测试
└── web/                       # 本机网页服务（FastAPI + 前端）
    ├── app.py                 # 联系人列表、批量 ZIP 导出接口
    └── static/index.html      # 网页界面
```

运行后，项目目录还会生成 `all_keys.json`（密钥）、`decrypted/`（解密数据库）和 `exports/`（命令行导出文件）；它们不属于项目源码。

---

## 一、环境要求

- **Windows + Python 3.10+**。安装 Python 时勾选 **Add Python to PATH**；在 PowerShell 中运行 `python --version` 检查。
- **微信 4.x**（`Weixin.exe`）。微信 3.x 的旧库结构不同，本工具不适用。

在 PowerShell 中进入项目目录，安装一次命令行和网页共用的依赖：

```powershell
cd 'D:\path\to\wechat-export-tool'
python -m pip install -r requirements.txt
```

把示例中的项目路径换成你实际存放项目的位置。

---

## 二、完整工作流

### 1. 找到微信数据目录

微信 4.x 的数据目录通常包含以下文件（自定义数据位置时可能在其他盘）：

```
<微信数据目录>\db_storage\
    ├── contact\contact.db
    ├── message\message_0.db  (message_1.db、message_2.db ...)
    ├── session\session.db
    └── ...
```

常见位置：`Documents\xwechat_files\<wxid>_<hash>\db_storage\`。密钥工具会尝试自动查找；找不到时在下一步用 `--db-dir` 指定。

### 2. 提取密钥并解密

保持微信已登录，在**项目目录**运行：

```powershell
python wcdb_key_tool_windows.py extract --decrypt
```

默认将密钥写入 `all_keys.json`，解密结果写入 `decrypted/`。自动查找数据目录失败时：

```powershell
python wcdb_key_tool_windows.py extract --decrypt --db-dir 'D:\微信数据\db_storage'
```

说明：

- Windows 下主路径是只读内存扫描；微信 4.1+ 会扫描 `Config.Cipher` 对象，一般无需退出重登。
- 微信长时间挂机可能清空内存密钥。扫描失败时可退出微信、重新登录后重试。
- 通常用当前用户权限即可提取；少数情况下需要管理员权限读取进程内存。

### 3. 导出聊天记录

以下命令默认读取**项目目录的 `decrypted/`**，导出到**项目目录的 `exports/`**：

```powershell
# 列出所有有聊天记录的联系人
python run.py list

# 导出单个联系人（支持备注、昵称、微信 ID 模糊匹配）
python run.py export --contact '张三'

# 导出全部联系人
python run.py export --all
```

如果解密数据库或导出目录在其他位置，使用 `--db`、`--out`：

```powershell
python run.py export --all --db 'D:\wechat-data\decrypted' --out 'D:\wechat-exports'
```

每个联系人会生成两个文件：

- `<姓名>_<username>_聊天记录.txt` — 人类可读文本（`[时间] 发送者: 内容`）。
- `<姓名>_<username>_聊天记录.json` — 结构化数据（联系人信息、时间范围和逐条消息）。

`run.py` 的默认数据和输出目录始终相对于项目目录；从其他工作目录调用脚本时也一样。

---

## 三、本机网页服务（可选）

网页提供联系人搜索、多选和批量 ZIP 下载。它直接读取解密后的本地目录，**无需注册、登录或上传数据库**。

在项目目录启动：

```powershell
python run.py web
```

打开终端显示的 `http://127.0.0.1:8765`；服务只监听本机 `127.0.0.1`，按 `Ctrl+C` 停止。指定其他数据库目录或端口：

```powershell
python run.py web --db 'D:\wechat-data\decrypted' --port 8766
```

**从旧版网页迁移**：如果数据原来放在 `DATA_ROOT\<用户名>\`，可直接将该目录传给 `--db`，例如 `python run.py web --db 'D:\wechat-export-data\alice'`。原数据和 `users.db` 不会被自动删除。

语音转文字（ASR）是可选高级功能，默认未部署，对应接口返回 503。启用时需自行准备 FunASR 的 llama.cpp 运行时与 SenseVoiceSmall 模型。

---

## 四、支持的消息类型

| 类型码 | 含义 |
|--------|------|
| 1 | 文本 |
| 3 | 图片 |
| 34 | 语音 |
| 37 | 好友验证 |
| 42 | 名片 |
| 43 | 视频 |
| 47 | 表情 |
| 48 | 位置 |
| 49 | 链接 / 文件 / 引用 |
| 10000 | 系统消息 |

- 图片、语音、表情等无文本内容时，导出文件只标注消息类型。
- 语音文件需先在微信中播放才会缓存到本地；默认无法转写。

---

## 五、常见问题

- **密钥扫描失败**：退出微信重新登录后重试；确认当前用户有读取微信进程内存的权限。
- **提示找不到 `decrypted/` 或数据库文件**：先完成解密；`--db` 应指向包含 `contact/contact.db` 和 `message/message_*.db` 的目录。
- **未找到有聊天记录的联系人**：确认 `--db` 指向解密后的目录，且联系人确实有消息记录。
- **提示缺少依赖或消息内容乱码**：在项目目录运行 `python -m pip install -r requirements.txt`；消息内容解压需要 `zstandard`。
- **只支持微信 4.x**：微信 3.x 使用不同目录结构与加密格式，不兼容。

解密数据库本身没有额外密码保护。请妥善保管 `all_keys.json`、`decrypted/` 和导出文件。

---

## 自动化验证

```powershell
python -B -m unittest discover -s tests -v
```

测试使用临时合成数据库和 ZIP，不会读取本机微信聊天内容。

---

## 致谢

- 密钥提取与解密：[wcdb-key-tool](https://github.com/TANGandXUE/wcdb-key-tool)（`wcdb_key_tool_windows.py` 来自该开源项目）。
