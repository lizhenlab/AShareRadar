from __future__ import annotations

from pathlib import Path
import subprocess
import textwrap

ROOT = Path(__file__).resolve().parents[1]


def _node(source: str) -> None:
    result = subprocess.run(["node", "--input-type=module", "-e", textwrap.dedent(source)], cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_requests_are_bounded_and_only_use_declared_scopes() -> None:
    _node("""
      import assert from 'node:assert/strict';
      import { financialJobRequest, activeFuyaoJob, verifiedStockObservation } from './static/js/fuyao-contracts.js';
      assert.deepEqual(financialJobRequest('financials', { symbols: '600519,000001.SZ,600519.SH' }),
        {kind:'financials', symbols:['600519.SH','000001.SZ'], index_symbols:[],period:'annual',limit:4});
      assert.deepEqual(financialJobRequest('history_full', { symbols:'invalid' }).symbols, []);
      assert.throws(() => financialJobRequest('financials', { symbols:'510300.SH' }));
      assert.throws(() => financialJobRequest('sectors', { indexSymbols:'885001' }));
      assert.throws(() => financialJobRequest('financials', { symbols:Array(101).fill('600519').join(',') }));
      assert.throws(() => financialJobRequest('unknown'));
      assert.throws(() => verifiedStockObservation({symbol:'000001.SZ'}, '600519.SH'));
      const accepted = {id:'a',status:'running'};
      assert.equal(activeFuyaoJob({jobs:[]}, accepted), accepted);
      assert.equal(activeFuyaoJob({jobs:[{id:'a',status:'completed'}]}, accepted), null);
    """)


def test_views_escape_financial_facts_and_keep_units_provenance_and_negative_values() -> None:
    _node("""
      import assert from 'node:assert/strict';
      import { renderFuyaoStock } from './static/js/fuyao-stock-view.js';
      import { renderFuyaoMarket } from './static/js/fuyao-market-view.js';
      const target={innerHTML:''};
      const period={period_end:'2025-12-31',period_type:'annual',currency:'CNY',alignment:'partial',
        metrics:[{key:'net_profit',label:'<img onerror=x>',raw_value:'-42',value:-42,unit:null,source_kind:'income'},
          {key:'cash',label:'现金',raw_value:null,value:null,unit:null,source_kind:'balance'}]};
      renderFuyaoStock(target,{financials:{symbol:'600519.SH',source:'同花顺扶摇',fetched_at:'2026-09-10',periods:[period],warnings:['时间未核实']}});
      assert.ok(target.innerHTML.includes('&lt;img onerror=x&gt;'));
      assert.ok(!target.innerHTML.includes('<img onerror=x>'));
      for (const phrase of ['-42','待核实','未返回','同花顺扶摇','2025-12-31','财务评分暂不生成']) assert.ok(target.innerHTML.includes(phrase));
      renderFuyaoMarket(target,{sentiment:{source:'扶摇',payload:{trade_date:'2026-09-10',pools:{'limit-up-pool':[]},anomalies:[{symbol:'600519.SH',text:'<script>x</script>'}]}}});
      assert.ok(!target.innerHTML.includes('<script>'));
      assert.ok(target.innerHTML.includes('未返回'));
      assert.ok(target.innerHTML.includes('提供者观点'));
    """)


def test_slow_previous_stock_response_cannot_replace_current_financial_panel() -> None:
    _node(_HARNESS + """
      let firstResolve;
      const fetchJson=async (url) => {
        if(url.endsWith('/status')) return status;
        if(url.includes('600519')) return await new Promise(resolve=>{firstResolve=resolve;});
        return observation('000001.SZ');
      };
      const controller=createFuyaoController({documentTarget:doc,getSymbol:()=>current,fetchJson});
      controller.bind();
      const old=controller.loadStock();
      current='000001.SZ';
      controller.resetStock(current);
      await controller.loadStock();
      firstResolve(observation('600519.SH'));
      await old;
      assert.ok(nodes.get('fuyaoStockPanel').innerHTML.includes('000001.SZ'));
      assert.ok(!nodes.get('fuyaoStockPanel').innerHTML.includes('600519.SH'));
      assert.equal(nodes.get('financialPanel').hidden,true);
      controller.destroy();
    """)


def test_reads_never_create_jobs_and_a_double_submit_creates_only_one() -> None:
    _node(_HARNESS + """
      const calls=[];
      let resolvePost;
      const fetchJson=async (url, options={})=>{
        calls.push([url,options.method||'GET']);
        if(options.method==='POST') return await new Promise(resolve=>{resolvePost=resolve;});
        if(url.endsWith('/status')) return status;
        if(url.endsWith('/market')) return {};
        return observation('600519.SH');
      };
      const controller=createFuyaoController({documentTarget:doc,getSymbol:()=>current,fetchJson});
      controller.bind();
      assert.deepEqual(calls, []);
      await controller.loadData();
      await controller.loadStock();
      assert.ok(calls.every(([,method])=>method==='GET'));
      const submitted=controller.submit('financials',true);
      await controller.submit('financials',true);
      assert.equal(calls.filter(([,method])=>method==='POST').length,1);
      current='000001.SZ';
      controller.resetStock(current);
      resolvePost({id:'1',kind:'financials',status:'running',completed:0,total:1});
      await submitted;
      assert.ok(nodes.get('fuyaoJobFeedback').textContent.includes('600519.SH'));
      assert.ok(!nodes.get('fuyaoStockPanel').innerHTML.includes('600519.SH'));
      controller.destroy();
    """)


def test_failed_bounded_reads_keep_saved_financial_market_and_job_content() -> None:
    _node(_HARNESS + """
      let fail=false;
      const optionsSeen=[];
      const fetchJson=async (url,options={})=>{
        optionsSeen.push(options);
        if(fail) throw new Error('请求超时，请稍后重试');
        if(url.endsWith('/status')) return {...status,jobs:[{id:'saved',kind:'financials',status:'completed',completed:1,total:1}]};
        if(url.endsWith('/market')) return {history:{version:'saved-version',symbols:2,trading_dates:[]}};
        return observation('600519.SH');
      };
      const controller=createFuyaoController({documentTarget:doc,getSymbol:()=>current,fetchJson});
      controller.bind();
      await controller.loadData();
      await controller.loadStock();
      const financialHtml=nodes.get('fuyaoStockPanel').innerHTML;
      const marketHtml=nodes.get('fuyaoMarketResults').innerHTML;
      fail=true;
      await controller.loadStock();
      assert.equal(nodes.get('fuyaoStockPanel').innerHTML,financialHtml);
      assert.equal(nodes.get('financialPanel').hidden,true);
      assert.ok(nodes.get('fuyaoStockFeedback').textContent.includes('已保留'));
      await controller.loadMarket();
      assert.equal(nodes.get('fuyaoMarketResults').innerHTML,marketHtml);
      assert.ok(nodes.get('fuyaoJobFeedback').textContent.includes('刷新本地状态'));
      await controller.readStatus();
      assert.ok(nodes.get('fuyaoStatusSummary').textContent.includes('请求超时'));
      assert.ok(nodes.get('fuyaoJobs').innerHTML.includes('已完成'));
      assert.ok(optionsSeen.every(options=>options.timeoutMs===12000));
      fail=false;
      await controller.loadMarket();
      assert.equal(nodes.get('fuyaoJobFeedback').textContent,'');
      controller.destroy();
    """)


def test_initial_market_read_displays_loading_and_timeout_recovery_message() -> None:
    _node(_HARNESS + """
      let rejectRead;
      const controller=createFuyaoController({documentTarget:doc,getSymbol:()=>current,
        fetchJson:async()=>await new Promise((resolve,reject)=>{rejectRead=reject;})});
      controller.bind();
      const pending=controller.loadMarket();
      assert.ok(nodes.get('fuyaoMarketResults').textContent.includes('正在读取'));
      rejectRead(new Error('请求超时，请稍后重试'));
      await pending;
      assert.ok(nodes.get('fuyaoMarketResults').textContent.includes('请求超时'));
      assert.ok(nodes.get('fuyaoMarketResults').textContent.includes('刷新本地状态'));
      controller.destroy();
    """)


_HARNESS = """
  import assert from 'node:assert/strict';
  import { createFuyaoController } from './static/js/fuyao-controller.js';
  const nodes = new Map();
  const doc={visibilityState:'visible',addEventListener(){},querySelectorAll(){return [];},getElementById(id){
    if(!nodes.has(id)) nodes.set(id,{innerHTML:'',textContent:'',value:'',hidden:false,classList:{toggle(){}}});
    return nodes.get(id);
  }};
  let current='600519.SH';
  const status={enabled:true,configured:true,jobs:[],download_hosts_configured:true,persistent_daily_requests:0};
  function observation(symbol){return {symbol,financials:{symbol,source:'扶摇',fetched_at:'2026-09-10',periods:[
    {period_end:'2025-12-31',period_type:'annual',alignment:'complete',metrics:[
      {key:'income',label:'营收',value:1,raw_value:'1',unit:null,source_kind:'income'}]}
  ]}};}
"""
