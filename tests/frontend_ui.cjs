// Exercises the actual inline Studio script in Chromium; only boot-time polling is
// disabled. HTTP fixtures stand in for the backend and capture the real API DTOs.
// Run: NODE_PATH="$CODEX_PRIMARY_RUNTIME_NODE_MODULES" node tests/frontend_ui.cjs
'use strict';
const fs = require('fs');
const path = require('path');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const file = path.resolve(__dirname, '../kit/nox_studio.html');
const source = fs.readFileSync(file, 'utf8');
const start = source.indexOf('// ---------------------------------------------------------------- пуск');
assert(start > 0, 'known bootstrap marker');
const fixtureHtml = source.slice(0, start) + '</script></body></html>';
for(const block of source.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)) new Function(block[1]);

const requests = [], browserErrors = [], results = [];
let trim = null, failMutation = false, nextFixture = false, soundPages = [], oldItems = [], failSave = false;
let roots = [{id:'ext1', name:'Игра <тест>', path:'D:\\sounds'}];
const soundTree = () => ({sections:[{sec:'существа',n:161,groups:[{grp:'волк',n:2}]}],roots,scan:{limited:true,visited:200,cap:200,errors:[]}});
const json = (route, data, status=200) => route.fulfill({status, contentType:'application/json', body:JSON.stringify(data)});
let browser, page;
const last = name => requests.filter(r => r.path === name).at(-1);
const count = name => requests.filter(r => r.path === name).length;
async function test(name, fn){
  try { await fn(); results.push({name, ok:true}); process.stdout.write('PASS ' + name + '\n'); }
  catch(e){ results.push({name, ok:false, error:e.stack}); process.stderr.write('FAIL ' + name + '\n' + e.stack + '\n'); throw e; }
}
async function init(){
  await page.goto('http://nox.test/');
  await page.evaluate(() => {
    const seg = (uid,id) => ({uid,id,role:'Тина',base_role:'Тина',text:'Проверяем прежнюю запись.',tech:'Проверяем прежнюю запись.',base_tech:'Проверяем прежнюю запись.',auto:'Проверяем прежнюю запись.',tempo:1,pause:0,after:0,flags:[],edits:[],draft:true,takes:[{tid:'t001',chosen:true,dur:10,why:[]}],locked:false,state:'edited',t0:0,t1:10});
    Object.assign(S,{n:1,part:{n:1,total:20,segs:[seg('u1',1),seg('u2',2)],cues:[],mix:{},lanes:[],locked:false,ver:'test'},roles:[{name:'Тина',voice:'tina.wav'}],sel:'u1',jobs:[],free:false,ed:null,book:{root:'test',title:'Test'}});
    $('#load').hidden = true;
  });
}
async function openTrim(cuts=[], t0=0, t1=10){
  await page.evaluate(async () => { closeDialog(); await trimDialog('u1','t001'); });
  await page.evaluate(({cuts,t0,t1}) => { TR.cuts=cuts; trimSet(t0,t1); }, {cuts,t0,t1});
}

(async () => {
  browser = await chromium.launch({executablePath:process.env.CHROMIUM_PATH || '/usr/bin/chromium',headless:true,args:['--no-sandbox']});
  page = await browser.newPage({viewport:{width:1440,height:1000}});
  page.on('pageerror', e => browserErrors.push(e.message));
  await page.route('http://nox.test/**', async route => {
    const request = route.request(), url = new URL(request.url()), p = url.pathname;
    if(p === '/') return route.fulfill({contentType:'text/html',body:fixtureHtml});
    const body = request.postData() ? JSON.parse(request.postData()) : null;
    requests.push({path:p,method:request.method(),body,query:Object.fromEntries(url.searchParams)});
    if(p === '/api/take_peaks') return json(route,{dur:10,peaks:Array(300).fill(.3),trim,trim0:[.1,9.9],next:{uid:'u2',id:2,text:'Следующая фраза.'}});
    if(p === '/api/take_trim'){ if(failMutation) return json(route,{error:'fixture mutation rejected'},409); trim = body.spans || (body.t0 == null ? null : [body.t0,body.t1]); return json(route,{ok:true}); }
    if(p === '/api/take_next') return json(route,nextFixture ? {uid:'u2',tid:'t001'} : {error:'fixture next stop'},nextFixture?200:409);
    if(p === '/api/sound_tree') return json(route,soundTree());
    if(p === '/api/sounds'){
      const offset = +(url.searchParams.get('offset') || 0); soundPages.push(offset);
      return json(route,{sounds:[{file:'ext1/wolf.wav',name:'wolf.wav',title:'Волк <script>window.bad=1</script>',kind:'amb',dur:12,lib:'ext',sub:'Существо & действие'}],total:161,offset,limit:80,next:offset<160?offset+80:null,scan:soundTree().scan});
    }
    if(p === '/api/sound_refresh') return json(route,{ok:true});
    if(p === '/api/sound_root'){ if(body.delete) roots = roots.filter(r => r.id!==body.id); else roots.push({id:'ext2',name:body.name,path:body.path}); return json(route,{files:3}); }
    if(p === '/api/old_voice') return json(route,{items:oldItems});
    if(p === '/api/old_voice_adopt') return json(route,{uid:body.uid,tid:'t002',selected:body.choose,source_raw:'Старый текст',target_raw:'Новый текст',exact:false});
    if(p === '/api/old_voice_auto') return json(route,{got:0,skipped:2});
    if(p === '/api/seg_wipe') return json(route,{takes:1});
    if(p === '/api/seg_reset') return json(route,{reset:{what:'текст'}});
    if(p === '/api/take') return json(route,{ok:true});
    if(p === '/api/seg') return json(route,failSave?{error:'save rejected'}:{ok:true},failSave?409:200);
    if(p === '/api/clean_takes') return json(route,{removed:2,backup:body.backup,reversible:true});
    if(p === '/api/log') return json(route,{last:0,ev:[]});
    if(p === '/api/cues_scan_all') return json(route,{parts:1,new:2,failed:[]});
    if(p === '/api/part') return json(route,await page.evaluate(()=>S.part));
    if(p.startsWith('/audio/') || p === '/peaks') return route.fulfill({status:200,body:Buffer.alloc(0)});
    return json(route,{error:'unhandled fixture '+p},404);
  });
  await init();

  await test('trim middle cut sends two real spans', async () => {
    await openTrim([[3,6]]); await page.getByRole('button',{name:'Применить',exact:true}).click();
    await page.waitForFunction(()=>$('#veil').hidden);
    assert.deepEqual(last('/api/take_trim').body.spans,[[0,3],[6,10]]);
  });
  await test('trim edge cut sends one retained span, never old bounds', async () => {
    trim=null; await openTrim([[0,6]]); await page.getByRole('button',{name:'Применить',exact:true}).click();
    await page.waitForFunction(()=>$('#veil').hidden);
    assert.deepEqual(last('/api/take_trim').body.spans,[[6,10]]);
  });
  await test('trim all-cut stays open and never mutates backend', async () => {
    trim=null; await openTrim([[0,10]]); const before=count('/api/take_trim');
    await page.getByRole('button',{name:'Применить',exact:true}).click();
    assert.equal(count('/api/take_trim'),before);
    assert.equal(await page.locator('#veil').isVisible(),true);
    assert.match(await page.locator('#banner').innerText(),/не осталось звука/);
  });
  await test('trim next follows identical one-span and zero-span contract', async () => {
    trim=null; await openTrim([[0,6]]); await page.getByRole('button',{name:/Применить и перейти/}).click();
    await page.waitForFunction(()=>document.querySelector('#banner').textContent.includes('fixture next stop'));
    assert.deepEqual(last('/api/take_next').body.spans,[[6,10]]);
    assert.equal(last('/api/take_next').body.t1,10);
    const before=count('/api/take_next'); await page.evaluate(()=>{TR.cuts=[[0,10]];trimSet(0,10)});
    await page.getByRole('button',{name:/Применить и перейти/}).click(); assert.equal(count('/api/take_next'),before);
  });
  await test('multiple saved spans roundtrip and reset keeps original file', async () => {
    trim=[[1,2],[4,6],[8,9]]; await page.evaluate(async()=>{closeDialog();await trimDialog('u1','t001')});
    assert.deepEqual(await page.evaluate(()=>trimKept()),trim);
    await page.locator('#trUncut').click(); assert.deepEqual(await page.evaluate(()=>trimKept()),[[1,9]]);
    await page.locator('#trAll').click(); await page.getByRole('button',{name:'Применить',exact:true}).click();
    await page.waitForFunction(()=>$('#veil').hidden);
    assert.equal(last('/api/take_trim').body.t0,null); assert.equal(last('/api/take_trim').body.t1,null);
  });
  await test('cut overlap and cancelled pointer preserve intervals', async () => {
    trim=null; await openTrim();
    await page.evaluate(()=>{trimAddCut(2,5);trimAddCut(4,7);TR.drag='c';TR.cs=0;TR.ce=9;$('#trCv').onpointercancel()});
    assert.deepEqual(await page.evaluate(()=>trimKept()),[[0,2],[7,10]]);
    assert.equal(await page.evaluate(()=>TR.drag),null);
  });
  await test('rejected trim retains editable dialog', async () => {
    failMutation=true; await openTrim([[3,6]]); await page.getByRole('button',{name:'Применить',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#banner').textContent.includes('fixture mutation rejected'));
    assert.equal(await page.locator('#veil').isVisible(),true); assert.equal(await page.getByRole('button',{name:'Применить',exact:true}).isEnabled(),true); failMutation=false;
  });
  await test('catalog Russian tabs, pagination, refresh and escaped names', async () => {
    await page.evaluate(()=>soundDialog('Каталог',snd=>{window.chosenSound=snd},'', ''));
    await page.locator('#slist [data-use]').waitFor(); assert.equal(await page.evaluate(()=>window.bad),undefined);
    await page.locator('#ssec [data-sec="существа"]').click(); await page.locator('#sgrp [data-grp="волк"]').click();
    await page.waitForFunction(()=>!document.querySelector('#sNext').disabled);
    await page.locator('#sNext').click(); await page.waitForFunction(()=>document.querySelector('#sPage').textContent.startsWith('81'));
    assert.equal(last('/api/sounds').query.sec,'существа'); assert.equal(last('/api/sounds').query.grp,'волк'); assert.equal(last('/api/sounds').query.offset,'80');
    await page.locator('#sRefresh').click(); await page.waitForFunction(()=>!document.querySelector('#sRefresh').disabled);
    assert.equal(count('/api/sound_refresh'),1); assert.match(await page.locator('#sinfo').innerText(),/Индекс ограничен/);
    if(process.env.NOX_FRONTEND_SHOTS) await page.screenshot({path:path.join(process.env.NOX_FRONTEND_SHOTS,'frontend_catalog.png')});
    await page.locator('#slist [data-use]').click();
    assert.equal(await page.evaluate(()=>window.chosenSound.kind),'amb'); assert.equal(await page.evaluate(()=>window.chosenSound.dur),12);
  });
  await test('root add captures entered path and manual disconnect can cancel', async () => {
    await page.evaluate(()=>soundRootDialog(()=>{})); await page.locator('#srPath').fill('D:\\new sounds'); await page.locator('#srName').fill('Новые звуки');
    await page.getByRole('button',{name:'Подключить',exact:true}).click(); await page.locator('[data-root-del="1"]').waitFor();
    assert.equal(last('/api/sound_root').body.path,'D:\\new sounds');
    const before=count('/api/sound_root'); page.once('dialog',d=>d.dismiss()); await page.locator('[data-root-del="1"]').click(); assert.equal(count('/api/sound_root'),before);
  });
  await test('old-voice compare exposes source and ambiguity, confirms selection', async () => {
    oldItems=[{sid:'s /one',tid:'t001',source_raw:'Прежний <img src=x onerror=window.bad=1>',text:'Переписанный текст',role:'Другая роль',ratio:.99,exact:false,ambiguous:true,dur:3}];
    await page.evaluate(async()=>{closeDialog();await oldVoiceDialog('u1')});
    const text=await page.locator('.oldvoices').innerText(); assert.match(text,/Прежний <img/); assert.match(text,/несколько подходящих/); assert.equal(await page.evaluate(()=>window.bad),undefined);
    await page.locator('[data-old-play]').click(); assert.match(await page.locator('#aux').getAttribute('src'),/sid=s%20%2Fone/);
    const before=count('/api/old_voice_adopt'); page.once('dialog',d=>d.dismiss()); await page.locator('[data-old-pick]').click(); assert.equal(count('/api/old_voice_adopt'),before);
    if(process.env.NOX_FRONTEND_SHOTS) await page.screenshot({path:path.join(process.env.NOX_FRONTEND_SHOTS,'frontend_old_voice.png')});
    page.once('dialog',d=>d.accept()); await page.locator('[data-old-pick]').click(); await page.waitForFunction(()=>$('#veil').hidden);
    assert.equal(last('/api/old_voice_adopt').body.choose,true);
  });
  await test('old-voice add keeps current selection and automatic mode is explicit', async () => {
    await page.evaluate(()=>oldVoiceDialog('u1')); await page.locator('[data-old-add]').click(); await page.waitForFunction(()=>$('#veil').hidden);
    assert.equal(last('/api/old_voice_adopt').body.choose,false);
    const before=count('/api/old_voice_auto'); page.once('dialog',d=>d.dismiss()); await page.evaluate(()=>oldVoiceAuto()); assert.equal(count('/api/old_voice_auto'),before);
    page.once('dialog',d=>d.accept()); await page.evaluate(()=>oldVoiceAuto()); assert.equal(last('/api/old_voice_auto').body.part,1);
  });
  await test('selected take delete visible, locked take delete absent', async () => {
    await page.evaluate(()=>{closeDialog();S.sel='u1';S.ed=null;drawInsp()});
    assert.equal(await page.locator('[data-del="t001"]').count(),1); assert.equal(await page.locator('#segReset').isDisabled(),true);
    assert.equal(await page.locator('#segWipe').count(),1);
    const before=count('/api/take'); page.once('dialog',d=>d.dismiss()); await page.locator('[data-del="t001"]').click(); assert.equal(count('/api/take'),before);
    await page.evaluate(()=>{segOf('u1').locked=true;drawInsp()}); assert.equal(await page.locator('[data-del="t001"]').count(),0);
    await page.evaluate(()=>{segOf('u1').locked=false;drawInsp()});
  });
  await test('wipe cancellation and request preserve draft distinction', async () => {
    const before=count('/api/seg_wipe'); page.once('dialog',d=>{assert.match(d.message(),/черновая запись конвейера останется/);d.dismiss()}); await page.locator('#segWipe').click(); assert.equal(count('/api/seg_wipe'),before);
    page.once('dialog',d=>d.accept()); await page.locator('#segWipe').click(); await page.waitForFunction(()=>document.querySelector('#banner').textContent.includes('Дубли перемещены'));
    assert.equal(last('/api/seg_wipe').body.uid,'u1');
  });
  await test('failed save keeps dirty text and blocks destructive followup', async () => {
    failSave=true; await page.evaluate(()=>{S.sel='u1';S.ed=null;drawInsp();S.ed.tokens=tokenize('Несохранённая правка');S.ed.dirty=true});
    const before=count('/api/seg_wipe'); page.once('dialog',d=>d.accept()); await page.evaluate(()=>wipeSeg('u1'));
    assert.equal(count('/api/seg_wipe'),before); assert.equal(await page.evaluate(()=>S.ed.dirty),true); failSave=false;
    await page.evaluate(()=>{S.ed=null});
  });
  await test('both cleanup modes stay reversible and never offer permanent deletion', async () => {
    for(const backup of [true,false]){
      await page.evaluate(value=>cleanDup(value),backup);
      const text=await page.locator('#dlg').innerText(); assert.match(text,/возможность отмены/); assert.doesNotMatch(text,/стирается насовсем/);
      await page.getByRole('button',{name:'Переместить в корзину',exact:true}).click(); await page.waitForFunction(()=>$('#veil').hidden);
      await page.waitForTimeout(20); assert.equal(last('/api/clean_takes').body.backup,backup);
    }
  });
  await test('all-part cue scan refreshes current part through GET', async () => {
    await page.evaluate(async()=>{closeDialog();await cueScanAll()});
    assert.equal(last('/api/part').method,'GET'); assert.equal(last('/api/part').query.n,'1');
  });
  assert.deepEqual(browserErrors,[],'no unhandled page errors');
  process.stdout.write(JSON.stringify({passed:results.length,failed:0,browserErrors,tests:results},null,2)+'\n');
})().catch(e=>{process.stderr.write(e.stack + '\n');process.exitCode=1}).finally(async()=>{if(browser)await browser.close()});
