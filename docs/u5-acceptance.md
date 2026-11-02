# U5 分析导航、状态与原文返回验收

执行日期：2026-11-01—02。开始状态为干净工作区，HEAD `142497b`。先读取 AGENTS.md、README、第7—8节规格及已有验收记录，保留 U1—U4、会话范围、阅读、反馈和文案行为。本次完成 U5-a、U5-b；后端统计、匹配、分母和回查接口未修改。

## 1. 已交付行为

- 保留“活动变化／词句与变化／讨论片段”。活动中直接进入时间、参与者、消息类型；词句中持续显示词句与关联、时期与提及变化、关键词时间对比。词频、短语、别名、新词可直接切换。
- 时期控件、两期消息总量、可用文字分母及变化同区。关键词在一张图中共享日期轴和纵轴尺度，最多6个一组；可点击日期点下钻。
- 排行默认12项，展开跟随主滚动，超过60项分页；移除结果卡片的纵向滚动。词句展开、片段分页和图表横向滚动保留。680px分析布局移除工作区外层纵向滚动；宽窗口证据侧栏独立滚动，窄窗口原文替换结果。
- 当前账号前端内存保存分区、区域、词句子类、局部输入、已应用参数、展开状态及位置。普通阅读和分析分别保存位置；共享查询变化重置片段页码、保留分析筛选，切账号清空分析结果和证据。
- 选词只高亮并加载相邻关联；“打开所选表达原文”“查看共同命中”明确进入原文。片段、变化、关联及命中使用稳定来源标识恢复滚动和按钮焦点；来源消失时回到有效面板位置。前后文单独保存，不覆盖本期／前期的命中页与内容。
- 最近一次成功结果及相同进行中请求可复用。完整key包含账号、共享查询、分析筛选、已应用参数、片段页、数据修订和规则修订。纯导航、展开、词句子类、前端粒度、趋势选择及打开／关闭证据不发完整统计；真实参数变化沿用原API，只接收最新有效响应。
- 分区、区域、词句子类具备 tablist/tab/tabpanel；左右键及Home/End仅移焦，Enter／空格激活。导航只自动滚动自身横轴，避免覆盖主区保存位置。

实现文件：`frontend/src/AnalysisWorkspace.tsx`、`main.tsx`、`analysis.css`、`style.css`；生产资源在 `web/static/`。README与易用性方案已同步。新增验证脚本见下文。

## 2. 隔离数据和运行条件

Windows，本机Chrome，持久测试配置 `D:/workspaces/pw-chrome-profile`；浏览器CSS视口为1366×860、900×600、680×860。源码测试解释器为 `D:/efficiency/anaconda3/envs/pytorch/python.exe`，便携入口使用包内CPython 3.11.9。Node使用已有依赖，先执行 `npm list -g --depth=0`；已有 `@playwright/cli@0.1.19`，未安装新依赖。

`scripts/content_analysis_fixture.py --u5` 在新目录生成两组合成会话，各144条，共288条；24位稳定发送者。两群合并的2026-10-19范围为168条可用文字，每群84条；前期2026-10-18每群28条。每10分钟一条，间隔参数5分钟产生多页片段。包括合成语音及预先保存的成功转写；没有执行转写。另生成空档案账号供切号回归。fixture在导入 `web.app` 前设置 `WECHAT_ARCHIVE_DATA_DIR`。

数据位置：`output/playwright/u5-baseline-data`、`u5-packaged-baseline`、`u5-final-data`、`u5-voice-final` 及各入口证据目录。均为本批合成材料。没有读取真实聊天、密钥或用户档案，也没有密钥扫描、在线转写和真实模型识别。

## 3. 改前／改后任务度量

改前首先在原源码静态资源运行任务；随后为消除异步scroll事件采集偏差，加入双requestAnimationFrame等待，在尚未同步的原基础版副本重测同一fixture、视口和任务。该副本仍提供基线资源 `index-DY3AodhE.js`／`index-CwxGAk6O.css`。改后在同步的基础版与完整语音版分别运行。

距离为 `.reading-scroll` 的累计绝对scrollTop变化，包含导航自动恢复，不是鼠标滚轮次数；H是该窗口的结果视口高度。时期任务从初始活动区进入词句区，将“两期提及变化”标题滚至可视；关键词任务在原文返回后单独清零距离计数，再定位关键词标题。两任务之间的原文阅读距离不混入发现距离。列表无须全部同屏。

| 窗口 | 改前H → 改后H | 时期发现距离：改前 → 改后（px / H） | 关键词发现距离：改前 → 改后（px / H） |
|---|---|---|---|
| 1366×860 | 606 → 619 | 1015 / 1.68 → 369 / 0.60 | 1127 / 1.86 → 381 / 0.62 |
| 900×600 | 346 → 359 | 1174 / 3.39 → 671 / 1.87 | 1574 / 4.55 → 1199 / 3.34 |
| 680×860 | 350 → 364 | 1248 / 3.57 → 622 / 1.71 | 1987 / 5.68 → 1303 / 3.58 |

改前时期任务有1次分区激活，关键词在同一长页继续滚动、无独立导航；两者发现展开数均0。改后两个目标从任意分区最多2次导航激活，发现展开数仍0。距离下降源自独立区域，未缩小字号。关键词改后距离包含从变化行自动回到关键词区域的位置恢复。

| 实际任务 | 改后结果 |
|---|---|
| 任意分区→时期与提及变化→两期原文→分析 | 三尺寸均通过；时期控件、总量、文字分母和变化属于同一比较区域；关闭原文回到来源行及焦点 |
| 任意分区→关键词时间对比→应用→切走返回 | 三尺寸均通过；已应用“合同、采样率”和未应用草稿分别保留；位置分别110、194.67、253.33px，返回误差<3px |
| 片段第二页→滚动→命中→前后文→命中→分析 | 三尺寸均通过；5分钟间隔、31–60页段、来源焦点保留；来源位置分别1736、1930、1970px，返回误差<3px |
| 选词→就近关联→共同命中→分析 | 三尺寸均通过；选词不打开证据，选中表达、词频子类、更多词句展开及关联来源焦点保留 |
| 带共享日期／关键词／成功转写条件／发送者筛选跨视图及普通阅读返回 | 条件与发送者芯片保持；纯切换完整分析请求增量0；各区滚动保持 |
| 键盘、长排行、宽窄布局 | 三尺寸均通过；Home/End、左右键、Enter／空格，焦点移动无请求；排行12→24随主区展开，无卡片纵向滚动；无页面横向溢出 |

改前普通阅读→分析每次新增1次完整统计，改后0。改后每尺寸主任务首次统计之外共4次完整请求，分别为应用关键词、关联anchor、应用5分钟间隔及片段翻页；纯切换、趋势选择、展开、回查与返回的增量均0。基础副本和完整语音副本的三尺寸任务均为120项断言通过，包含各起点进入关键词的检查。

补充请求／状态回归25项通过：同一进行中请求跨阅读和区域切换仍只有1次；相同成功结果返回复用；共享查询保留发送者；延迟旧anchor响应不能覆盖新anchor；关联500错误就地显示并可重试；证据第二页随宽窄切换保留；手选比较时段仅1次完整请求；本期和前期分别翻至51–55/55后，从前期上下文返回仍保持双页及前期来源焦点；共享日期变化从片段第二页回首页且仅1次完整请求；空档案账号无旧统计／证据，切回后建立新的分析内存；从窄窗证据经普通阅读返回分析，保留分析来源位置且不重发统计。该脚本共15次完整请求，包含真实筛选变化和一次主动错误重试；合成500为预期控制，不是未处理故障。

## 4. 实际执行的主要命令

以下命令从仓库根目录运行。每次fixture的 `--output` 使用新目录；重复执行时应换目录。

```powershell
git status --short
git log -8 --oneline
npm list -g --depth=0
where.exe npx
D:/efficiency/anaconda3/envs/pytorch/python.exe scripts/content_analysis_fixture.py --u5 --output output/playwright/u5-baseline-data --serve
playwright-cli -s=u5 open http://127.0.0.1:8767 --browser chrome --persistent --profile D:/workspaces/pw-chrome-profile --headed
playwright-cli -s=u5 run-code --filename tests/probe_u5_baseline.js
D:/efficiency/anaconda3/envs/pytorch/python.exe scripts/content_analysis_fixture.py --app "dist/微信聊天档案 基础版/app" --u5 --output output/playwright/u5-packaged-baseline --serve --port 8768
playwright-cli -s=u5 goto http://127.0.0.1:8768
playwright-cli -s=u5 run-code --filename tests/probe_u5_baseline.js
D:/efficiency/anaconda3/envs/pytorch/python.exe scripts/verify_u5.py
npm --prefix frontend run build
D:/efficiency/anaconda3/envs/pytorch/python.exe scripts/content_analysis_fixture.py --u5 --output output/playwright/u5-final-data --serve
playwright-cli -s=u5 goto http://127.0.0.1:8767
playwright-cli -s=u5 run-code --filename tests/probe_u5_ui.js
playwright-cli -s=u5 run-code --filename tests/probe_u5_requests.js
D:/efficiency/anaconda3/envs/pytorch/python.exe scripts/build_portable.py --output "dist/微信聊天档案 基础版" --resources-only
D:/efficiency/anaconda3/envs/pytorch/python.exe scripts/build_portable.py --output "dist/微信聊天档案 完整语音版" --resources-only
playwright-cli -s=u5 goto http://127.0.0.1:8768
playwright-cli -s=u5 run-code --filename tests/probe_u5_ui.js
D:/efficiency/anaconda3/envs/pytorch/python.exe scripts/content_analysis_fixture.py --app "dist/微信聊天档案 完整语音版/app" --u5 --output output/playwright/u5-voice-final --serve --port 8769
playwright-cli -s=u5 goto http://127.0.0.1:8769
playwright-cli -s=u5 run-code --filename tests/probe_u5_ui.js
playwright-cli -s=u5 run-code --filename tests/probe_u5_requests.js
D:/efficiency/anaconda3/envs/pytorch/python.exe scripts/probe_u5_portable.py --package "dist/微信聊天档案 基础版" --output "output/playwright/U5 基础版交付"
D:/efficiency/anaconda3/envs/pytorch/python.exe scripts/probe_u5_portable.py --package "dist/微信聊天档案 完整语音版" --output "output/playwright/U5 完整语音版交付"
D:/efficiency/anaconda3/envs/pytorch/python.exe scripts/probe_u5_portable.py --package "dist/微信聊天档案 完整语音版" --entry 启动微信聊天档案.cmd --output "output/playwright/U5 根目录交付"
git diff --check
```

`verify_u5.py` 在临时数据目录导入web模块，执行 `test_analysis`、`test_analysis_matching`、`test_search`、`test_lexical_exploration`，33项全部通过。覆盖完整范围、两期分母、片段完整时间线、词句匹配和原文回查；前端TypeScript及Vite生产构建通过。测试环境有已安装Starlette/httpx弃用提示，未影响测试结果。

开发中实际发现并修正：导航的纵向scrollIntoView干扰保存位置；普通阅读与分析的恢复互相覆盖；上下文渲染替换后旧DOM焦点失效；查询变化重置片段页码可能先发旧页请求；窄窗工作区额外滚动。入口探测第一次遗漏资源URL中的 `/static/`，修正探测脚本后重新运行通过，未以该次失败当作入口通过。

## 5. 构建与运行副本

最终资源：`web/static/assets/index-C5jw9rDu.js`、`index-SBOwaav_.css`。两个现有便携目录均以 `--resources-only` 更新程序、静态资源、README及资源清单，保留既有运行时和持久数据。无依赖变化，无ZIP重打。

| 实际入口 | 结果 |
|---|---|
| `dist/微信聊天档案 基础版/启动微信聊天档案.cmd` | ShellExecute按双击关联启动，原生窗口loaded，随包子进程退出码0，实际HTTP索引及JS/CSS逐字节等于本次构建，自动关闭且服务停止 |
| `dist/微信聊天档案 完整语音版/启动微信聊天档案.cmd` | 同上；没有加载模型或进行转写 |
| 仓库根目录 `启动微信聊天档案.cmd` | 实际定位完整语音版，使用传入的合成数据目录；相同资源检查及停止通过 |

入口路径均包含中文与空格。入口测试不等同于全部原生鼠标操作；完整导航／滚动／焦点／请求任务由Chrome实际加载相应运行副本静态资源完成。源码、运行副本及发行ZIP的结论分别记录。

保留证据：`output/playwright/u5-ui-result.json`、`u5-request-result.json`、`u5-voice-ui-result.json`、三个 `u5-baseline-*.png`、三个 `u5-after-*.png`，以及各入口目录的 `result.json`、合成数据和 `smoke-test.json`。临时fixture服务与本次浏览器会话在收尾停止；原生smoke服务已自行停止。

## 6. 停止条件与剩余范围

第7.5节U5实际任务及本批状态／请求回归均通过，本批停止。分析内存不跨账号或程序重启持久化。发行ZIP未更新或验证；干净Windows／缺原生运行库、真实微信加密WAL和真实模型仍属于已有外部验收范围，不影响本次隔离U5交付。内容分析第二、三阶段算法未实施。
