# 首版工作台验收记录

日期：2026-10-18。范围：0 + A + B + C。测试数据全部为隔离合成数据；未自动对真实微信提取密钥、导入或转写。原有 `design-research/` 保留。

## 已执行的功能验收

| 路径 | 实际结果 |
|---|---|
| 查找会话 → 固定阅读 → 日期定位 → 整段导出 | 1366×768 Chrome；第二群消息日期 2023-11-17 至 19 得到 3 条；实际下载 JSON 的 total=3，记录都在该时段 |
| 两字检索 → 上下文 → 日图下钻 → 命中导出 | “报销”得到 5 条，含 1 条明确标注的成功语音转写；上下文能读到“合同”和会议记录，仍不扩大命中；点击 2023-11-18 得到 1 条，实际下载 JSON 的 count=total=1，记录查询条件和生成时间 |
| 维护更新 → 保留条件并刷新 | 仅在临时源库增加 1 条同日“报销”，界面“导入所选”完成后，当前关键词和日期不变，结果/总数/日图从 1 一起更新为 2 |
| 打开、选择、关注分离 | 只点击会话不会勾选；勾选第二群后搜索 pending，仍显示“已选 1 / 当前筛选外 1”，完整选择可展开 |
| 阅读状态持久化 | 服务端按账号保存会话、页码、消息日期、滚动；浏览器从 127.0.0.1 切换 localhost 的新来源后，恢复第二群及 2023-11-17 至19日期。单元测试另验证重建 Manager 与拒绝旧账号迟到写入 |
| 错误与键盘焦点 | 开始日期晚于结束日期时显示可操作错误；清除后恢复；数据设置抽屉 Escape 关闭后焦点回到“数据设置”按钮 |
| 静态交付 | TypeScript 检查与 Vite 生产构建通过，产物在 web/static；用户电脑不需要 Node |

截图位于项目 `output/playwright/`：`01-search.png`、`03-hit-export.png`、`04-refreshed.png`、`05-reader-final.png`、`06-context-final.png`。上下文目标已验证位于阅读区可见范围；切换分析清除上下文状态并回到顶部。最终浏览器控制台没有错误。

## 自动化与性能

```powershell
python -m unittest discover -s tests -v
python scripts/benchmark_search.py
```

完整测试运行 89 项，全部通过，耗时 35.0 秒；随后新增便携模型隔离测试，连同两项启动器回归共 3 项通过（累计 90 项不同测试）。新增搜索测试覆盖：字面两字中文、成功转写与来源、失败/非语音转写不命中、仅正文而非任意 JSON 字段、分页与日期、稳定上下文、同一只读快照、维护提交后刷新、三种命中导出及 HTTP 契约。原有账号隔离、历史保留、导入事务、维护串行及维护期间读取测试保留。

5 万条临时合成消息，748 条命中、35 日桶，重复 5 次：搜索整体中位约 208 ms，上下文定位中位约 78 ms。本次消除了统计的重复扫描；未引入全文分词或搜索索引。该结果仅代表本机、单会话合成规模，不外推全库搜索。

## 桌面与发布证据

基础包使用官方 CPython 3.11.9 Windows x64 embed，未复制开发机 Conda 环境。中文、空格目录中实际启动 EdgeChromium 窗口，服务就绪后加载静态页面，包内 Python 子进程成功导入 sqlite3 / zstandard，返回码 0。启动后自动关闭的 smoke 证据写在包的 `local_data/logs/smoke-test.json`，不纳入发布 ZIP。

`scripts/probe_native.py` 使用真实 pywebview JavaScript Promise 桥、loopback 合成响应和 Windows 原生保存对话框，保存到中文空格文件名。`build-cache/native evidence/native-probe.json` 确认 `saved=true`、`bytes_equal=true`、请求端点为 `/api/export-search`。桥的内部对象使用私有属性，避免 pywebview 递归遍历 .NET 窗口对象。

桌面单元测试覆盖中文空格路径解析、成功落盘、请求失败不覆盖原文件、维护/保存期间阻止关闭，以及 Windows 同数据目录重复实例锁。

发布由 `scripts/build_portable.py` 的程序清单组装；ZIP 由 `scripts/zip_portable.py` 按清单创建，排除 `local_data/` 和 `__pycache__/`。基础和语音依赖锁、依赖版本清单、CPython/第三方许可及原生依赖说明随包提供。基础 Python、语音 Python、静态资源、模型、档案和日志均有独立路径，不依赖当前工作目录。

真实原生目录选择、打开保存结果及维护中关闭拦截均已通过，证据为 `build-cache/native evidence 2/native-probe.json`。

完整 CPU 语音链路已实际通过：包内 voice-runtime + torch 2.5.1+cpu（CUDA 编译版本为空）+ pilk Tencent SILK + 随包 FFmpeg + models/small.pt。Windows SAPI 生成合成中文，编码为 SILK 后离线识别，准确输出“这是一段离线语音测试。今天处理报销记录。”，耗时约 15.2 秒。证据在 `build-cache/voice evidence/voice-probe.json`。未使用真实微信语音或外部识别服务。

升级保留档案已在隔离的“中文 空格便携升级”目录验证：随包运行时导入合成账号的 2 条消息并保存阅读状态，退出后覆盖 app 程序文件，再启进程，账号 ID、消息及阅读状态完全一致（`preserved=true`）。证据在 `build-cache/upgrade evidence final/upgrade-probe.json`。

## 外部验收

未取得无 Python、Node、Conda 的全新 Windows 虚拟机或实体机，因此不宣称干净机器验收已通过。所需最小步骤如下：

1. 将最终 ZIP 复制到 Windows 10/11 x64 新机器，解压到包含中文和空格的可写路径。先断网双击；如提示原生依赖缺失，使用包内 prerequisites 离线安装器，按系统要求重启，再双击启动。
2. 使用合成或用户自行许可的测试库，走通目录选择、身份确认、导入、两字查询、上下文、日下钻和两种导出，核对实际文件并打开结果。
3. 运行维护时关闭窗口，应提示等待且窗口保留；完成后关闭，确认自身本机服务退出。再次双击及重复双击应分别恢复档案和提示已有实例。
4. 完整版断网执行一段已知内容的 SILK 语音转写，确认 CPU 可运行、无需模型下载，并核对文字；基础版语音组件缺失时应给出安装完整包的说明。
5. 关闭应用，保留 local_data 替换 app/runtime 等程序资源，再启动，核对账号、消息数量、转写和阅读位置仍在。升级包不得覆盖原 local_data。

材料篮、AI 总结、知识图谱、关系评分、安装向导和自动更新不属于本版范围。
