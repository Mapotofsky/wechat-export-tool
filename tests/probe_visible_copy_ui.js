// Run with playwright-cli against tests/ui_fixture_server.py --prepared only.
async (page) => {
  const api = (path, options) => page.request.get(page.url().split('/').slice(0,3).join('/') + path, options);
  const runtime = await (await api('/api/runtime-summary')).json();
  if (!runtime.data.includes('wechat_ui_bootstrap_')) throw Error('Requires the isolated UI fixture');
  for(const route of ['**/api/task-preview','**/api/asr-status','**/api/data-status','**/api/tasks/synthetic-copy-task/cancel'])await page.unroute(route);
  await page.reload();
  const result = [];
  const phase = page.url().split('#')[1] || 'prepare';
  const dialog = () => page.getByRole('dialog');
  const button = (name) => page.getByRole('button', {name, exact: true});
  const summary = (name) => page.locator('summary').filter({hasText: new RegExp('^' + name + '$')});
  const close = async () => {if (await button('关闭面板').isVisible()) await button('关闭面板').click()};
  const check = (condition, label) => {if (!condition) throw Error(label); result.push(label)};
  const capture = async (name) => {
    const size = await page.evaluate(() => ({width: innerWidth, content: document.documentElement.scrollWidth}));
    check(size.content <= size.width, name + ': no page overflow');
    await page.screenshot({path: 'output/playwright/copy-' + name + '.png'});
  };
  await page.getByRole('button', {name:'数据设置', exact:true}).waitFor();
  await page.setViewportSize({width:900,height:600});
  await close();

  if (phase === 'prepare') {
    await button('数据设置').click();
    await summary('通讯录与本人身份').click();
    await button('准备通讯录').click();
    await page.getByText('准备此账号的通讯录和会话名单，不导入聊天正文。', {exact:true}).waitFor();
    check(!(await dialog().innerText()).includes('0 个会话'), 'prepare does not imply an empty conversation selection');
    await capture('prepare-900');
    await dialog().getByRole('button',{name:'确认执行',exact:true}).click();
    await page.getByRole('region',{name:'准备通讯录',exact:true}).getByText('已完成',{exact:true}).waitFor();
    check(await button('重试此任务').isVisible(), 'completed preparation offers retry and settings');
    await close();
    await button('数据设置').click();
    await summary('通讯录与本人身份').click();
    await page.getByLabel('微信号 / 内部 username / 昵称 / 备注').fill('public_id');
    await button('查找身份映射').click();
    await page.getByText(/找到 1 个身份条目/).waitFor();
    await page.getByRole('checkbox',{name:'我确认选中的内部身份属于自己'}).check();
    await button('确认这是我并保存').click();
    await page.getByText(/身份已保存/).waitFor();
    await close();
    await button('数据设置').click();
    await button('更新全部会话').click();
    await page.getByRole('region',{name:'所需磁盘空间'}).waitFor();
    check(await page.getByText('尚未计入全部用量，无法确定空间是否足够。',{exact:true}).isVisible(), 'actual capacity does not promise sufficient space');
    check(!(await page.getByRole('region',{name:'所需磁盘空间'}).innerText()).includes('已确定需要复制'), 'copy calculation hidden until expanded');
    await capture('capacity-900');
    await dialog().getByRole('button',{name:/^确认执行/}).click();
    await page.getByRole('region',{name:'更新全部档案',exact:true}).getByText('已完成',{exact:true}).waitFor();
    check(!(await dialog().innerText()).includes('未变化或重复的来源记录'), 'source counters hidden from task results');
    await summary('处理明细与诊断').click();
    await page.getByText(/未变化或重复的来源记录/).waitFor();
    await capture('task-success-900');
    await close();

    let shortage = false;
    await page.route('**/api/task-preview', async route => {
      const response = await route.fetch();
      const body = await response.json();
      body.capacity = {copy_bytes:20e6,retained_bytes:40e6,blocked:shortage,files:['D:/合成文件/很长的中文路径/'.repeat(7)+'contact.db'],unknown:['合成容量状态：尚未估算全部增长。'],volumes:[
        {volume:'D:\\',free_bytes:shortage?8e6:80e6,necessary_bytes:20e6,estimated_bytes:40e6,unknown_growth:false,purposes:['聊天数据副本'],paths:['D:/合成数据']},
        {volume:'C:\\',free_bytes:null,necessary_bytes:0,estimated_bytes:0,unknown_growth:true,purposes:['导入时的临时文件'],paths:['C:/合成临时目录']}]};
      await route.fulfill({response,json:body});
    });
    await button('数据设置').click();await button('更新全部会话').click();
    await page.getByText('预计新增占用：约 40 MB（部分估算）',{exact:true}).waitFor();
    check(await page.getByText('当前可用：未知',{exact:true}).isVisible(), 'unknown capacity remains unknown');
    await page.setViewportSize({width:680,height:600});await capture('capacity-partial-680');
    await summary('查看空间估算明细').click();await summary('本次准备的文件（1）').click();await capture('capacity-details-680');
    await close();shortage=true;await button('数据设置').click();await button('更新全部会话').click();
    await page.getByText('空间不足，请先释放相应磁盘的空间，再重试。',{exact:true}).waitFor();
    check(await dialog().getByRole('button',{name:/^确认执行/}).isDisabled(), 'known shortage still blocks execution');
    await capture('capacity-shortage-680');await close();await page.unroute('**/api/task-preview');
  }

  if (phase === 'voice') {
    let checks=0,kind='missing';
    await page.route('**/api/asr-status', async route => {
      checks++;
      const status=await (await api('/api/data-status')).json();
      const provider=status.config.asr_provider||'local';
      const info=kind==='missing'?{available:false,models:[],missing:['语音运行时'],reason:'未找到语音运行时，请使用完整语音包补齐语音组件。'}:
        kind==='failed'?{available:false,models:[],reason:'合成检查失败：退出码 1；'+('中文长路径与诊断信息。'.repeat(40))}:
        {available:true,silk:true,whisper:true,models:['D:/合成模型/small.pt'],device:'cpu'};
      await route.fulfill({json:{...info,provider,python:'D:/合成语音运行时/python.exe'}});
    });
    await button('数据设置').click();await summary('语音识别设置').click();
    const voice=page.getByRole('region',{name:'语音识别配置'});
    check(checks===0,'opening voice settings does not check tools');
    await voice.getByRole('button',{name:'检查已保存设置'}).click();await voice.getByText('语音识别尚未就绪',{exact:true}).waitFor();
    await capture('voice-missing-900');
    kind='failed';await voice.getByRole('button',{name:'检查已保存设置'}).click();await voice.getByText('工具检查未通过。请展开检查详情，按原因处理后重新检查。',{exact:true}).waitFor();
    await voice.locator('summary').filter({hasText:'语音检查详情'}).click();await page.setViewportSize({width:680,height:600});await capture('voice-error-details-680');
    kind='ready';await voice.getByRole('button',{name:'检查已保存设置'}).click();await voice.getByText('本地语音识别已就绪',{exact:true}).waitFor();
    const before=checks;await page.waitForTimeout(1800);check(checks===before,'status polling does not repeat tool checks');
    await voice.getByLabel('识别方式').selectOption('openai');await voice.getByLabel('完整 API 地址').fill('http://127.0.0.1:1/synthetic-never-called');await voice.getByLabel('在线模型名').fill('synthetic');
    await voice.getByRole('button',{name:'保存语音设置'}).click();await voice.getByText('语音设置已保存',{exact:true}).waitFor();
    check(checks===before,'saving online settings does not check or transcribe');
    await voice.getByRole('button',{name:'检查已保存设置'}).click();await voice.getByText('本机音频处理已就绪',{exact:true}).waitFor();
    check(await voice.getByText(/在线服务能否识别将在转写时确认/).isVisible(),'online readiness limited to local audio tools');await capture('voice-online-680');
    await voice.getByLabel('识别方式').selectOption('local');await voice.getByRole('button',{name:'保存语音设置'}).click();await voice.getByText('语音设置已保存',{exact:true}).waitFor();
    await close();await page.unroute('**/api/asr-status');
  }

  if (phase === 'projects') {
    const before=await (await api('/api/messages',{params:{username:'second@chatroom'}})).json();
    await page.getByRole('checkbox',{name:'选择 第二群 second@chatroom'}).check();await button('管理').click();
    const name='合成文案检查项目：合同、报销与历史聊天记录';
    await page.getByLabel('新项目名称').fill(name);await button('新建项目').click();
    await page.waitForFunction(name=>Array.from(document.querySelectorAll('label')).some(l=>l.firstChild?.textContent==='项目名称'&&l.querySelector('input')?.value===name),name);
    await button('将这些会话加入项目').click();
    await page.getByRole('heading',{name:'项目中的会话（1）',exact:true}).waitFor();await button('删除项目').click();
    await page.getByText(/删除项目“合成文案检查项目.*聊天记录仍保留/).waitFor();await page.setViewportSize({width:680,height:600});await capture('project-delete-680');
    await button('确认删除项目').click();await page.getByLabel('管理项目').selectOption('followed');
    const after=await (await api('/api/messages',{params:{username:'second@chatroom'}})).json();
    check(before.total===after.total&&JSON.stringify(before.messages)===JSON.stringify(after.messages),'project deletion preserves messages');await close();
  }

  if (phase === 'analysis') {
    await page.getByRole('button',{name:/^第二群 8 群聊/}).click();await button('分析').click();await button('筛选与别名').waitFor({timeout:30000});await button('筛选与别名').click();
    const settings=page.locator('dialog[open]');await settings.getByRole('heading',{name:'词句筛选与别名组'}).waitFor();
    check(!(await settings.innerText()).includes('PMI≥'),'precise algorithms hidden initially');check((await settings.innerText()).includes('关闭后这些词可重新入榜'),'effects visible before changing switches');
    await capture('analysis-settings-900');await page.setViewportSize({width:680,height:600});await capture('analysis-settings-680');
    await settings.getByRole('checkbox',{name:'隐藏纯数字',exact:true}).check();await settings.getByRole('button',{name:'保存筛选设置'}).click();await settings.getByText(/设置已保存/).waitFor();
    await settings.getByRole('button',{name:'添加别名组'}).click();await settings.getByText(/请填写组名和要合并的表达/).waitFor();
    await settings.getByLabel('别名组名').fill('合同相关');await settings.getByLabel('别名表达').fill('合同\n合约');await settings.getByRole('button',{name:'添加别名组'}).click();await settings.getByRole('checkbox',{name:'启用 合同相关'}).waitFor();
    const state=await (await api('/api/data-status')).json();const saved=await (await api('/api/analysis/settings',{params:{account_id:state.account.id}})).json();
    check(saved.options.hide_numbers&&saved.aliases.some(a=>a.name==='合同相关'),'analysis settings and aliases saved through existing API');
    await settings.getByRole('button',{name:'删除',exact:true}).click();await settings.getByRole('checkbox',{name:'隐藏纯数字',exact:true}).uncheck();await settings.getByRole('button',{name:'保存筛选设置'}).click();
    await settings.getByRole('button',{name:'关闭',exact:true}).click();
    check((await (await api('/api/messages',{params:{username:'second@chatroom'}})).json()).total===8,'analysis choices preserve original message total');
  }

  if (phase === 'tasks') {
    const state=await (await api('/api/data-status')).json();
    let task={id:'synthetic-copy-task',account_id:state.account.id,action:'transcribe',status:'running',stage:'提取与语音转写',activity:'正在发送并等待当前语音响应',unit:'条语音',done:0,total:1,cancellable:true,reason:'',next_step:'',selected_count:1,selected_usernames:['second@chatroom'],scope:'selected'};
    await page.route('**/api/data-status',async route=>{const response=await route.fetch();await route.fulfill({response,json:{...(await response.json()),task}})});
    let stopped=false;await page.route('**/api/tasks/synthetic-copy-task/cancel',async route=>{const body=route.request().postDataJSON();stopped=body.account_id===state.account.id;task={...task,status:'cancelling'};await route.fulfill({json:task})});
    await button('处理任务').click();await page.getByRole('region',{name:'语音转写'}).waitFor();await page.getByText('0 / 1 条语音',{exact:true}).waitFor();await capture('task-running-900');
    await button('停止任务').click();await page.getByText(/已保存的转写保留；正在识别的在线语音需等待返回或超时/).waitFor();check(stopped,'stop request keeps task and account scope');
    await capture('task-stopping-900');task={...task,status:'cancelled',stage:'已停止',activity:'',cancellable:false,result:{remaining:1},next_step:'已保存的结果保留，可以重试此任务'};await button('重试此任务').waitFor();
    task={...task,status:'success',stage:'完成',done:1,result:{success:0,reused:1,skipped:0,missing:0,failed:0},next_step:'可继续阅读或导出'};await page.getByText('已有转写，未重复识别',{exact:true}).waitFor();await capture('task-reused-900');
    task={...task,status:'failed',stage:'转写结束，存在失败语音',failed_stage:'提取与语音转写',reason:'无法读取这条合成语音，请检查语音设置后重试。'+('合成的长错误说明。'.repeat(30)),result:{failed:1},next_step:'已有成功转写保留，请查看失败原因后重试'};await page.getByText(/无法读取这条合成语音/).waitFor();await page.setViewportSize({width:680,height:600});await capture('task-error-680');
    await summary('处理明细与诊断').click();await page.getByText('失败时正在：处理语音转写',{exact:true}).waitFor();
    let diagnostic='';await page.evaluate(()=>{window.__diagnosticCopy='';Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.__diagnosticCopy=text}}})});
    await button('复制本次诊断').click();await page.getByText('本次诊断已复制',{exact:true}).waitFor();diagnostic=await page.evaluate(()=>window.__diagnosticCopy);
    check(JSON.parse(diagnostic).task.failed_stage==='提取与语音转写','copied diagnostic preserves raw failed_stage');
    task={...task,id:'synthetic-copy-save',status:'running',action:'update',stage:'提交消息档案',failed_stage:'提取与语音转写',done:1,total:1,unit:'个会话',cancellable:false,reason:'',result:undefined,next_step:''};await page.getByRole('progressbar',{name:'保存聊天记录',exact:true}).waitFor();
    check(await page.getByText('1 / 1 个会话',{exact:true}).isVisible(),'stage display preserves progress units');check(await button('请等待当前操作完成').isDisabled(),'save phase still cannot stop');
    await page.evaluate(()=>{window.__diagnosticCopy=''});await button('复制本次诊断').click();await page.waitForFunction(()=>window.__diagnosticCopy&&JSON.parse(window.__diagnosticCopy).task.stage==='提交消息档案');diagnostic=await page.evaluate(()=>window.__diagnosticCopy);check(JSON.parse(diagnostic).task.stage==='提交消息档案','copied diagnostic preserves raw stage');
    await page.evaluate(()=>window.dispatchEvent(new Event('archive-close-request')));await page.getByText('保存聊天记录',{exact:true}).waitFor();check(!(await dialog().innerText()).includes('提交消息档案'),'exit wait uses the same stage display');await capture('exit-stage-680');await button('继续使用 / 等待').click();
    await page.getByRole('banner').getByRole('button',{name:/导入 \/ 更新档案/}).click();task={...task,stage:'正在清理任务资源'};await page.getByText('正在结束任务',{exact:true}).waitFor();check(await page.getByRole('status').filter({hasText:'进行中'}).isVisible(),'cleanup is still in progress, not completed');
    task={...task,action:'discover',stage:'发现历史会话',done:2,total:0,unit:'个会话',diagnostic_code:'partial'};await page.getByText('已找到 2 个会话；总量尚未确定，请稍候…',{exact:true}).waitFor();check(!(await dialog().innerText()).includes('部分完成'),'partial code does not mark a running task complete');check(await page.getByRole('progressbar').count()===0,'unknown total does not invent percentage');
    task={...task,id:'synthetic-partial-key',action:'extract',status:'success',stage:'部分密钥已保存',reason:'',selected_count:0,selected_usernames:[],diagnostic_code:'partial',result:{keys_saved:true},next_step:'已为 1/3 个数据库验证并保存密钥；可继续准备通讯录，缺少所需密钥时再核对目录并重试'};await page.getByText('部分完成',{exact:true}).waitFor();check(!(await dialog().innerText()).includes('查看此次处理的会话（0）'),'account task does not show an empty conversation list');await capture('task-partial-680');
    await page.unroute('**/api/data-status');await page.unroute('**/api/tasks/synthetic-copy-task/cancel');await close();
  }
  if (phase === 'resources') {
    await button('数据设置').click();const resources=summary('数据位置、语音准备与升级');await resources.focus();await page.keyboard.press('Enter');await page.getByText('数据目录：'+runtime.data,{exact:true}).waitFor();
    check(await resources.locator('..').getByText('还没有检查结果。使用已保存的设置检查，不会开始转写。',{exact:true}).isVisible(),'resource summary waits for explicit tool check');
    const details=summary('程序与工具详情');await details.focus();await page.keyboard.press('Enter');check(await details.evaluate(e=>e.parentElement.open),'program details open with keyboard');await page.setViewportSize({width:680,height:600});await capture('resources-680');await details.focus();await page.keyboard.press('Enter');check(!(await details.evaluate(e=>e.parentElement.open)),'program details close with keyboard');await page.keyboard.press('Escape');check(await dialog().count()===0,'settings closes with Escape');
  }
  if (phase === 'progress') {
    const state=await (await api('/api/data-status')).json();
    let task={id:'synthetic-progress',account_id:state.account.id,action:'update_all',status:'running',stage:'复制快照',done:320000000,total:400000000,unit:'字节',activity:'',cancellable:true,reason:'',next_step:'',selected_count:1,selected_usernames:['second@chatroom'],scope:'all'};
    await page.route('**/api/data-status',async route=>{const response=await route.fetch();await route.fulfill({response,json:{...(await response.json()),task}})});
    await button('处理任务').click();await page.getByText('当前文件：320 MB / 400 MB',{exact:true}).waitFor();
    check(await page.locator('.task-activity').count()===0,'empty activity has no placeholder');
    check(await page.locator('.task-progress').evaluate(e=>e.getBoundingClientRect().height<100),'copy progress has no blank fixed height');await capture('progress-copy-900');
    task={...task,stage:'解密主数据库页面',done:800,total:1200,unit:'页'};await page.getByText('当前文件：800 / 1,200 页',{exact:true}).waitFor();
    task={...task,stage:'稳定快照',done:2,total:5,unit:'个数据库'};await page.getByText('2 / 5 个数据库',{exact:true}).waitFor();
    task={...task,stage:'验证加密 WAL 提交边界',done:48,total:0,unit:'帧'};await page.getByText('已处理 48 帧；总量尚未确定，请稍候…',{exact:true}).waitFor();check(await page.getByRole('progressbar').count()===0,'unknown frame total has count but no percentage');
    task={...task,stage:'导入消息',done:2,total:5,unit:'个会话',activity:'导入消息（会话 3/5，已处理 512 条；复用 120，新增 8，变化 2）'};await page.getByText('正在导入聊天记录（会话 3/5，本会话已读取来源记录 512 条；累计未变化或重复来源 120，累计新增消息 8，累计更新消息 2）',{exact:true}).waitFor();await page.setViewportSize({width:680,height:600});await capture('progress-import-680');
    task={...task,activity:'正在处理 D:\\合成路径\\'+('中文 空格目录\\'.repeat(18))+'message_3.db'};await page.locator('.task-activity').filter({hasText:'message_3.db'}).waitFor();await capture('progress-long-680');
    task={...task,status:'success',stage:'完成',activity:'',done:5};await page.getByText('已完成',{exact:true}).waitFor();check(await page.locator('.task-activity').count()===0,'finished task does not retain old activity');
    await page.unroute('**/api/data-status');await close();
  }
  return {phase,checks:result};
}
