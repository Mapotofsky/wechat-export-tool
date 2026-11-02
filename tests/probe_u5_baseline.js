async page => {
  const results=[];
  for(const size of [{width:1366,height:860},{width:900,height:600},{width:680,height:860}]){
    await page.setViewportSize(size);await page.reload();
    let requests=0;const count=r=>{if(r.url().endsWith('/api/analysis'))requests++};page.on('request',count);
    await page.getByRole('button',{name:'分析',exact:true}).click();
    await page.locator('.analysis-metrics').waitFor();
    await page.evaluate(()=>{const s=document.querySelector('.reading-scroll');window.u5={distance:0,last:s.scrollTop};s.addEventListener('scroll',()=>{window.u5.distance+=Math.abs(s.scrollTop-window.u5.last);window.u5.last=s.scrollTop})});
    await page.getByRole('button',{name:'词句与变化',exact:true}).click();
    await page.getByRole('heading',{name:'两期提及变化',exact:true}).scrollIntoViewIfNeeded();await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));
    const change=await page.evaluate(()=>({distance:window.u5.distance,height:document.querySelector('.reading-scroll').clientHeight}));
    await page.locator('.change-list>button').first().click();await page.locator('.analysis-evidence').waitFor();
    await page.getByRole('button',{name:'收起阅读',exact:true}).click();
    await page.evaluate(()=>{window.u5.distance=0;window.u5.last=document.querySelector('.reading-scroll').scrollTop});
    await page.getByRole('heading',{name:'关键词时间对比',exact:true}).scrollIntoViewIfNeeded();await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));
    const keywords=await page.evaluate(()=>({distance:window.u5.distance,height:document.querySelector('.reading-scroll').clientHeight}));
    await page.getByRole('button',{name:'阅读',exact:true}).click();
    const before=requests;await page.getByRole('button',{name:'分析',exact:true}).click();
    await page.locator('.analysis-metrics').waitFor();
    results.push({size,change,keywords,returnRequests:requests-before,changeNavigation:1,keywordNavigation:0,discoveryExpansions:0});
    await page.screenshot({path:`output/playwright/u5-baseline-${size.width}.png`});page.off('request',count);
  }
  return results;
}
