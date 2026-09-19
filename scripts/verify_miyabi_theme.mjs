// Actual browser colors on an isolated local service; no external model calls.
import { createRequire } from 'node:module';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || path.join(os.homedir(), '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'));
const base = process.env.THEME_TEST_URL || 'http://127.0.0.1:8895';
const output = 'docs/verification';
const workspace = process.env.THEME_TEST_WORKSPACE || 'C:/Users/Public/harness-miyabi-qa/workspace';
const data = process.env.THEME_TEST_DATA || path.dirname(workspace);
fs.mkdirSync(workspace, { recursive: true });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const report = { date: new Date().toISOString(), base, checks: [], colors: [], failures: [], errors: [] };
page.on('pageerror', e => report.errors.push(e.message));
page.setDefaultTimeout(12000);

async function audit(name) {
  const result = await page.evaluate(() => {
    const rows = [];
    const parse = c => (c.match(/[\d.]+/g) || []).map(Number);
    const blend = (fg, bg) => { const a = fg[3] ?? 1; return fg.slice(0, 3).map((v, i) => a*v+(1-a)*bg[i]); };
    const luminance = rgb => rgb.slice(0,3).map(v => { v/=255; return v<=.04045 ? v/12.92 : ((v+.055)/1.055)**2.4; }).reduce((n,v,i)=>n+v*[.2126,.7152,.0722][i],0);
    const ratio = (a,b) => { const x=luminance(a),y=luminance(b); return (Math.max(x,y)+.05)/(Math.min(x,y)+.05); };
    const visible = el => {
      if (!el.getClientRects().length || getComputedStyle(el).visibility !== 'visible') return false;
      for (let p=el;p;p=p.parentElement) { const s=getComputedStyle(p); if(s.display==='none'||+s.opacity===0) return false; }
      return !el.closest('script,style,svg,option,.brand-mark span');
    };
    const background = el => {
      const ancestors=[]; for(let p=el;p;p=p.parentElement) ancestors.unshift(p);
      return ancestors.reduce((bg,p)=>blend(parse(getComputedStyle(p).backgroundColor),bg),[255,255,255]);
    };
    const add = (el,text,pseudo=null) => {
      if(!visible(el)||!text.trim()) return;
      const style=getComputedStyle(el,pseudo), bg=background(el), fg=blend(parse(style.color),bg);
      rows.push({selector:el.tagName.toLowerCase()+'.'+el.className, text:text.trim().slice(0,55), pseudo, foreground:style.color, background:bg.map(Math.round), ratio:+ratio(fg,bg).toFixed(3), disabled:el.matches(':disabled,[aria-disabled="true"]')});
    };
    const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
    for(let n=walker.nextNode();n;n=walker.nextNode()) add(n.parentElement,n.textContent);
    for(const el of document.querySelectorAll('input:not([type=hidden]):not([type=checkbox]),textarea')) {
      add(el,el.value||el.placeholder,el.value?null:'::placeholder');
    }
    for(const el of document.querySelectorAll('select')) add(el,el.selectedOptions[0]?.textContent||'');
    for(const el of document.querySelectorAll('.inline-editor:empty')) add(el,el.dataset.placeholder||'', '::before');
    return rows;
  });
  report.colors.push({ name, samples: result.length, minimum: Math.min(...result.map(r=>r.ratio)), rows: result });
  report.failures.push(...result.filter(r=>r.ratio<4.5).map(r=>({state:name,...r})));
  report.checks.push(name);
}

try {
  const ticket=execFileSync('.venv/Scripts/harness.exe',['--data-dir',data,'pair'],{encoding:'utf8'}).trim();
  await page.goto(base+'/#launch='+encodeURIComponent(ticket));
  await page.getByRole('button', {name:/添加项目/}).click();
  await page.getByLabel('项目名称').fill('霜青工作台 · 配色验收');
  await page.getByLabel('源文件夹 1',{exact:true}).fill(workspace);
  await audit('project-dialog');
  await page.getByRole('button',{name:'保存并提交',exact:true}).click();
  await page.getByRole('button',{name:'＋ 新建第一个会话',exact:true}).last().click();
  await page.getByRole('textbox',{name:'消息输入框',exact:true}).waitFor();
  await audit('desktop-empty-disabled-send-placeholder');
  await page.screenshot({path:`${output}/miyabi-desktop.png`});
  const editor=page.getByRole('textbox',{name:'消息输入框',exact:true});
  await editor.fill('整理本项目的设计决策，并标注需要验证的事项。');
  await editor.focus();
  await audit('composer-focus-enabled-send');
  const focus=await page.locator('.composer').evaluate(el=>({border:getComputedStyle(el).borderColor,shadow:getComputedStyle(el).boxShadow}));
  assert.notEqual(focus.shadow,'none');
  await page.getByRole('button',{name:/执行模式：/}).click();
  await audit('mode-menu-selected');
  await page.getByRole('radio',{name:/Plan/}).hover();
  await audit('mode-menu-hover');
  await page.screenshot({path:`${output}/miyabi-mode-menu.png`});
  await page.keyboard.press('Escape');
  await page.getByRole('button',{name:/会话模型：/}).click();
  await audit('model-menu');
  await page.locator('.model-search').fill('不存在的模型');
  await audit('model-menu-empty');
  await page.keyboard.press('Escape');
  await page.getByRole('button',{name:/设置与诊断/}).click();
  await page.locator('.model-profile').first().waitFor();
  await audit('settings-model-list');
  await page.locator('.model-setup-card').waitFor();
  await audit('settings-model-form');
  await page.screenshot({path:`${output}/miyabi-settings.png`,fullPage:true});
  await page.locator('.settings-nav button').filter({hasText:'MCP'}).click();
  await page.locator('.json-editor').waitFor();
  await audit('settings-mcp-json');
  await page.getByRole('button',{name:'返回工作台',exact:true}).click();
  await page.setViewportSize({width:390,height:844});
  await audit('mobile-workbench');
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.screenshot({path:`${output}/miyabi-mobile.png`});
  await page.getByRole('button',{name:/会话模型：/}).click();
  await audit('mobile-model-menu');
  const box=await page.locator('.model-menu .composer-popover').boundingBox();
  assert.ok(box.x>=0&&box.x+box.width<=390);
  await page.keyboard.press('Escape');
  await page.getByRole('button',{name:'打开导航',exact:true}).click();
  await audit('mobile-dark-sidebar');
  await page.screenshot({path:`${output}/miyabi-mobile-sidebar.png`});
  await page.locator('.sidebar button').evaluateAll(buttons=>buttons.forEach(b=>{b.dataset.wasDisabled=String(b.disabled);b.disabled=true;}));
  await audit('dark-sidebar-disabled-controls-fixture');
  await page.locator('.sidebar button').evaluateAll(buttons=>buttons.forEach(b=>{b.disabled=b.dataset.wasDisabled==='true';delete b.dataset.wasDisabled;}));

  // Supplemental visual fixture uses shipped CSS. It does not claim backend state coverage.
  await page.setViewportSize({width:1440,height:1000});
  await page.evaluate(() => {
    document.body.innerHTML=`<main style="padding:32px;max-width:1000px;margin:auto">
      <h1>状态与阅读配色验收</h1><p class="muted">静态样例 · 使用实际构建 CSS</p>
      <div class="notice">普通提示 <small>辅助说明仍清晰</small></div>
      <div class="notice error">错误：上传未完成，请重试 <small>失败原因与解决方法</small></div>
      <div class="notice success">成功：配置已保存 <small>已验证的能力</small></div>
      <div class="actions"><span class="status status-running">运行中</span><span class="status status-completed">已完成</span><span class="status status-waiting_user">等待确认</span><span class="status status-failed">失败</span><span class="status">已取消</span></div>
      <article class="interaction-card"><div class="eyebrow">需要确认</div><h3>请审阅即将执行的操作</h3><p>权限范围与审批说明</p><button class="primary">允许一次</button> <button class="danger">拒绝</button></article>
      <article class="markdown-text"><h2>阅读与代码</h2><p>正文、<a href="#">文档链接</a>与 <code>inline_code</code>。</p><blockquote>引用与补充资料保持独立层次。</blockquote><pre><code>const theme = "miyabi";\nconsole.log(theme);</code></pre><div class="markdown-table"><table><tr><th>字段</th><th>内容</th></tr><tr><td>主题</td><td>霜青</td></tr></table></div></article>
      <div class="composer"><div class="inline-editor" contenteditable="true">附件：<span class="inline-attachment">设计说明.md <span class="attachment-status">就绪</span><button>×</button></span><span class="inline-attachment failed">示意图.png <span class="attachment-status">上传失败</span><button>重试</button></span></div></div>
      <div class="model-test-result failure"><span>!</span><div><strong>验证失败</strong><p>检查网络后重试</p></div></div>
      <div class="actions"><button>普通按钮</button><button class="primary">主要操作</button><button class="primary" disabled>禁用操作</button><input placeholder="输入提示文字" /></div>
      <aside class="inspector" style="position:static;width:auto"><div class="inspector-body"><div class="metric-grid"><div><span>模型调用</span><strong>12</strong></div></div><button class="list-card selected">已选记录 <small>记录来源与时间</small></button></div></aside>
    </main>`;
  });
  await audit('supplemental-status-markdown-attachment-inspector-fixture');
  await page.getByRole('button',{name:'主要操作',exact:true}).hover();
  await audit('primary-hover-fixture');
  await page.screenshot({path:`${output}/miyabi-states.png`,fullPage:true});
  report.nonText = await page.evaluate(() => {
    const s=getComputedStyle(document.documentElement);
    const lum = hex => hex.match(/[a-f\d]{2}/gi).map(v=>parseInt(v,16)/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((n,v,i)=>n+v*[.2126,.7152,.0722][i],0);
    return [['focus','paper'],['focus','soft'],['focus','selected'],['control-border','paper'],['control-border','canvas'],['control-border','soft'],['on-dark-accent','sidebar-bg'],['on-dark-accent','sidebar-selected']].map(([fg,bg])=>{
      const a=lum(s.getPropertyValue('--'+fg).trim()),b=lum(s.getPropertyValue('--'+bg).trim());
      return {fg,bg,ratio:+((Math.max(a,b)+.05)/(Math.min(a,b)+.05)).toFixed(3)};
    });
  });
  assert.ok(report.nonText.every(r=>r.ratio>=3),JSON.stringify(report.nonText));
  await page.emulateMedia({forcedColors:'active'});
  await page.keyboard.press('Tab');
  await page.getByRole('button',{name:'主要操作',exact:true}).focus();
  assert.notEqual(await page.getByRole('button',{name:'主要操作',exact:true}).evaluate(el=>getComputedStyle(el).outlineStyle),'none');
  report.checks.push('forced-colors-keyboard-focus');
  assert.equal(report.errors.length,0);
  assert.equal(report.failures.length,0,JSON.stringify(report.failures,null,2));
  report.completed = true;
} finally {
  if(!report.completed) {
    await page.screenshot({path:`${output}/miyabi-debug.png`});
    fs.writeFileSync(`${output}/miyabi-debug.txt`,await page.locator('body').innerText());
  }
  fs.writeFileSync(`${output}/miyabi-browser.json`,JSON.stringify(report,null,2));
  await browser.close();
}
console.log(JSON.stringify({checks:report.checks.length,samples:report.colors.reduce((n,s)=>n+s.samples,0),failures:report.failures.length,minimum:Math.min(...report.colors.map(s=>s.minimum))}));
