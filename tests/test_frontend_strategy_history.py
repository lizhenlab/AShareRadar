from __future__ import annotations

import json

import pytest

from tests.test_frontend_strategy_recovery import _run_node


PRELUDE = r'''
const historyContext = (id = 9, version = 1) => ({ execution_id:id, strategy_id:7,
  strategy_version:version, strategy_fingerprint:'a'.repeat(64), execution_fingerprint:'b'.repeat(64),
  market_scan_run_id:3, source_snapshot_digest:'c'.repeat(64), source_snapshot_seal_origin:'publication',
  cost_rule_fingerprint:'d'.repeat(64), rule_version:'full-market-score-v5', data_as_of:'2026-09-04T15:00:00+08:00',
  data_date:'2026-09-04', kind:'latest_scan', status:'no_trade', point_in_time:true, created_at:'2026-09-04T16:00:00+08:00' });
const savedDraft = (id = 9, version = 1) => ({...draft(),context:historyContext(id,version)});
const historyPage = (page = 1, total = 1, ids = [9]) => ({items:ids.map(id=>historyContext(id)),page,
  page_size:100,total,page_count:Math.ceil(total/100)});
const savedPage = (page = 1, total = 1, ids = [7]) => ({items:ids.map(id=>strategy(id)),page,
  page_size:100,total,page_count:Math.ceil(total/100)});
const savedPlan = (id = 9) => ({...historyContext(id),plan_id:4,plan_digest:'e'.repeat(64),
  orders:[],disclaimers:['仅供纸面研究'],status:'no_trade'});
function reader(override = () => undefined) {
  const requests=[];
  const controller=makeController(async (url,options={})=>{
    requests.push({url,method:options.method || 'GET'});
    const changed=override(url,options);
    if(changed !== undefined) return changed;
    const path=new URL(url,'http://local').pathname;
    if(path.endsWith('/compile')) return {normalized_spec:JSON.parse(options.body).spec,
      fingerprint:'a'.repeat(64),execution_plan:{executable:true}};
    if(path==='/api/strategy-lab/strategies') return savedPage();
    if(path==='/api/strategy-lab/strategies/7') return strategy(7);
    if(path==='/api/strategy-lab/strategies/7/executions') return historyPage();
    if(path.endsWith('/versions')) return {items:[{revision:1,name:spec.name,fingerprint:'a'.repeat(64)}],total:1};
    if(path.endsWith('/evidence')) return null;
    if(path==='/api/strategy-lab/executions/9') return savedDraft();
    if(path.endsWith('/candidates')) return {items:[],execution_id:9,page:1,page_size:50,total:0,page_count:0};
    if(path.endsWith('/simulation-plan')) return null;
    throw new Error('unexpected request '+url);
  });
  return {controller,requests};
}
async function openStrategy(controller) {
  element('strategySavedSelect').value='7';
  await click('strategyLoad');
  assert.equal(controller.state.historyPage?.total,1);
}
'''


def run(body: str) -> None:
    _run_node(PRELUDE + body)


def test_saved_list_paginates_to_archived_record_without_replacing_draft() -> None:
    run(r'''
      const {controller,requests}=reader(url=>{
        if(!url.includes('/strategies?')) return undefined;
        const page=Number(new URL(url,'http://local').searchParams.get('page'));
        const payload=savedPage(page,101,page===1?Array.from({length:100},(_,i)=>i+2):[1]);
        if(page===2) payload.items[0].archived=true;
        return payload;
      });
      loadEditor(controller,strategy(7));
      element('strategyName').value='未保存的草案';
      await controller.loadStrategies();
      await click('strategyListNext');
      assert.equal(controller.state.strategyPage.page,2);
      assert.equal(controller.state.strategies.length,1);
      assert.equal(controller.state.strategies[0].archived,true);
      assert.match(element('strategySavedSelect').innerHTML,/value="1"/);
      assert.match(element('strategySavedSelect').innerHTML,/当前载入 · 不在本页/);
      assert.equal(controller.state.strategy.strategy_id,7);
      assert.equal(element('strategyName').value,'未保存的草案');
      assert.match(element('strategyListPage').textContent,/第 2 \/ 2 页 · 共 101/);
      assert.equal(element('strategyListNext').disabled,true);
      await click('strategyListPrev');
      assert.deepEqual(requests.map(item=>Number(new URL(item.url,'http://local').searchParams.get('page'))),[1,2,1]);
    ''')


def test_failed_list_page_does_not_advance_or_replace_selection_and_retries_same_page() -> None:
    run(r'''
      let fail=true;
      const {controller,requests}=reader(url=>{
        const page=Number(new URL(url,'http://local').searchParams.get('page'));
        if(page===2&&fail){fail=false;throw new Error('第二页暂不可读');}
        return savedPage(page,101,page===1?[7]:[8]);
      });
      await controller.loadStrategies();
      const previous=element('strategySavedSelect').innerHTML;
      await click('strategyListNext');
      assert.equal(controller.state.strategyPage.page,1);
      assert.equal(element('strategySavedSelect').innerHTML,previous);
      assert.match(status().textContent,/第二页暂不可读/);
      await click('strategyListNext');
      assert.equal(controller.state.strategyPage.page,2);
      assert.deepEqual(requests.map(item=>new URL(item.url,'http://local').searchParams.get('page')),['1','2','2']);
    ''')


@pytest.mark.parametrize('field,value', [('page',2),('page_size',20),('page_count',4),('total',-1)])
def test_invalid_list_page_metadata_is_rejected(field: str, value: int) -> None:
    run(r'''
      const {controller}=reader(()=>({...savedPage(),[FIELD]:VALUE}));
      await controller.loadStrategies();
      assert.equal(controller.state.strategyPage,null);
      assert.match(status().textContent,/分页身份或计数异常/);
    '''.replace('FIELD',json.dumps(field)).replace('VALUE',str(value)))


def test_single_saved_execution_and_paper_plan_reopen_without_any_write() -> None:
    run(r'''
      const {controller,requests}=reader(url=>url.endsWith('/simulation-plan')?savedPlan():undefined);
      await openStrategy(controller);
      const editor=element('strategyName').value;
      const original=controller.state.strategy;
      requests.length=0;
      await click('strategyExecutionLoad');
      assert.equal(controller.state.execution.context.execution_id,9);
      assert.equal(controller.state.executionReadOnly,true);
      assert.match(element('strategyLifecycleContent').innerHTML,/纸面委托草案 #4/);
      assert.match(element('strategyExecutionContext').textContent,/扫描 #3/);
      assert.equal(controller.state.strategy,original);
      assert.equal(element('strategyName').value,editor);
      assert.equal(element('strategyCreateSimulation').disabled,true);
      await click('strategyCreateSimulation');
      assert.deepEqual(requests.map(item=>item.method),['GET','GET','GET']);
      assert.match(status().textContent,/只读.*当前编辑草案保持不变/);
    ''')


def test_history_read_preserves_edited_draft_and_current_revision() -> None:
    run(r'''
      const {controller,requests}=reader();
      await openStrategy(controller);
      controller.state.strategy=strategy(7,2);
      element('strategyName').value='下一版尚未保存';
      const original=controller.state.strategy;
      requests.length=0;
      await click('strategyExecutionLoad');
      assert.equal(controller.state.execution.context.strategy_version,1);
      assert.equal(controller.state.strategy,original);
      assert.equal(controller.state.strategy.revision,2);
      assert.equal(element('strategyName').value,'下一版尚未保存');
      assert.equal(element('strategyCreateSimulation').disabled,true);
      assert.ok(requests.every(item=>item.method==='GET'));
    ''')


def test_archived_strategy_history_is_readable_but_cannot_execute_or_create_plan() -> None:
    run(r'''
      const {controller,requests}=reader(url=>url==='/api/strategy-lab/strategies/7'?{...strategy(7),archived:true}:undefined);
      await openStrategy(controller);
      requests.length=0;
      await click('strategyExecutionLoad');
      assert.equal(controller.state.execution.context.execution_id,9);
      assert.equal(element('strategyExecuteLatest').disabled,true);
      assert.equal(element('strategyExecuteReplay').disabled,true);
      assert.equal(element('strategyCreateSimulation').disabled,true);
      await controller.execute('latest_scan');
      await click('strategyCreateSimulation');
      assert.ok(requests.every(item=>item.method==='GET'));
    ''')


def test_missing_saved_plan_is_explicit_and_does_not_generate_one() -> None:
    run(r'''
      const {controller,requests}=reader();
      await openStrategy(controller);
      requests.length=0;
      await click('strategyExecutionLoad');
      assert.match(element('strategyLifecycleContent').textContent,/尚未保存.*不会自动生成/);
      assert.equal(status().dataset.kind,'ready');
      assert.ok(requests.every(item=>item.method==='GET'));
    ''')


def test_failed_plan_read_preserves_execution_and_can_be_retried_without_write() -> None:
    run(r'''
      let fail=true;
      const {controller,requests}=reader(url=>{
        if(!url.endsWith('/simulation-plan')) return undefined;
        if(fail){fail=false;throw new Error('暂不可读');}
        return savedPlan();
      });
      await openStrategy(controller);
      requests.length=0;
      await click('strategyExecutionLoad');
      assert.equal(controller.state.execution.context.execution_id,9);
      assert.match(element('strategyLifecycleContent').textContent,/读取失败/);
      assert.match(status().textContent,/已读取执行.*同步未完成/);
      await click('strategyExecutionLoad');
      assert.match(element('strategyLifecycleContent').innerHTML,/纸面委托草案 #4/);
      assert.ok(requests.every(item=>item.method==='GET'));
    ''')


@pytest.mark.parametrize('field,value', [('execution_id',99),('strategy_id',99),('strategy_version',2),('execution_fingerprint','f'*64)])
def test_mismatched_history_detail_preserves_prior_display(field: str, value: object) -> None:
    run(r'''
      const {controller}=reader(url=>url==='/api/strategy-lab/executions/9'
        ?{...savedDraft(),context:{...historyContext(),[FIELD]:VALUE}}:undefined);
      await openStrategy(controller);
      const previous=savedDraft(8);
      controller.state.execution=previous;
      await click('strategyExecutionLoad');
      assert.equal(controller.state.execution,previous);
      assert.match(status().textContent,/身份与所选历史不一致/);
    '''.replace('FIELD',json.dumps(field)).replace('VALUE',json.dumps(value)))


def test_history_page_failure_keeps_old_page_and_retries_without_skipping() -> None:
    run(r'''
      let fail=true;
      const {controller,requests}=reader(url=>{
        if(!url.includes('/strategies/7/executions?')) return undefined;
        const page=Number(new URL(url,'http://local').searchParams.get('page'));
        if(page===2&&fail){fail=false;throw new Error('历史分页失败');}
        return historyPage(page,101,page===1?[9]:[8]);
      });
      element('strategySavedSelect').value='7';
      await click('strategyLoad');
      await click('strategyHistoryNext');
      assert.equal(controller.state.historyPage.page,1);
      assert.equal(element('strategyExecutionSelect').value,'9');
      await click('strategyHistoryNext');
      assert.equal(controller.state.historyPage.page,2);
      assert.equal(element('strategyExecutionSelect').value,'8');
      assert.deepEqual(requests.filter(item=>item.url.includes('/strategies/7/executions?'))
        .map(item=>new URL(item.url,'http://local').searchParams.get('page')),['1','2','2']);
    ''')


@pytest.mark.parametrize('target', ['candidates','simulation-plan'])
def test_mismatched_secondary_response_is_not_rendered(target: str) -> None:
    run(r'''
      const {controller}=reader(url=>{
        if(TARGET==='candidates'&&url.includes('/candidates?')) return {items:[],execution_id:99,page:1};
        if(TARGET==='simulation-plan'&&url.endsWith('/simulation-plan')) return savedPlan(99);
        return undefined;
      });
      await openStrategy(controller);
      await click('strategyExecutionLoad');
      assert.equal(controller.state.execution.context.execution_id,9);
      assert.match(status().textContent,/同步未完成/);
      if(TARGET==='candidates') assert.equal(controller.state.candidatePage,null);
      else assert.match(element('strategyLifecycleContent').textContent,/读取失败/);
    '''.replace('TARGET',json.dumps(target)))


def test_stale_execution_detail_cannot_replace_new_context() -> None:
    run(r'''
      const pending=deferred();
      const {controller}=reader(url=>url==='/api/strategy-lab/executions/9'?pending.promise:undefined);
      await openStrategy(controller);
      const operation=click('strategyExecutionLoad');
      controller.state.strategy=strategy(42);
      pending.resolve(savedDraft());
      await operation;
      assert.equal(controller.state.execution,null);
      assert.equal(controller.state.strategy.strategy_id,42);
    ''')


def test_stale_secondary_responses_cannot_replace_new_execution_or_announce_old_success() -> None:
    run(r'''
      const pending=deferred();
      const {controller}=reader(url=>url.endsWith('/simulation-plan')?pending.promise:undefined);
      await openStrategy(controller);
      const operation=click('strategyExecutionLoad');
      for(let i=0;i<20&&controller.state.execution===null;i++) await new Promise(resolve=>setImmediate(resolve));
      const newer=savedDraft(11);
      controller.state.execution=newer;
      element('strategyLifecycleContent').textContent='新执行的视图';
      pending.resolve(savedPlan());
      await operation;
      assert.equal(controller.state.execution,newer);
      assert.equal(element('strategyLifecycleContent').textContent,'新执行的视图');
      assert.doesNotMatch(status().textContent,/已读取执行 #9/);
    ''')


def test_busy_history_read_rejects_a_second_read_and_paging_without_duplicate_work() -> None:
    run(r'''
      const pending=deferred();
      const {controller,requests}=reader(url=>url==='/api/strategy-lab/executions/9'?pending.promise:undefined);
      await openStrategy(controller);
      const operation=click('strategyExecutionLoad');
      assert.equal(element('strategyExecutionLoad').disabled,true);
      assert.equal(element('strategyListRefresh').disabled,true);
      await click('strategyExecutionLoad');
      pending.resolve(savedDraft());
      await operation;
      assert.equal(requests.filter(item=>item.url==='/api/strategy-lab/executions/9').length,1);
      assert.equal(element('strategyExecutionLoad').disabled,false);
    ''')


@pytest.mark.parametrize('remaining_total', [0, 1])
def test_saved_list_recovers_when_current_page_is_removed_by_retention(remaining_total: int) -> None:
    run(r'''
      let cleaned=false;
      const {controller,requests}=reader(url=>{
        const page=Number(new URL(url,'http://local').searchParams.get('page'));
        if(!cleaned) return savedPage(page,101,page===1?[7]:[8]);
        return savedPage(page,TOTAL,page===1&&TOTAL?[7]:[]);
      });
      await controller.loadStrategies();
      await click('strategyListNext');
      assert.equal(controller.state.strategyPage.page,2);
      cleaned=true;
      await click('strategyListRefresh');
      assert.equal(controller.state.strategyPage.page,1);
      assert.equal(controller.state.strategyPage.total,TOTAL);
      assert.equal(element('strategyListPrev').disabled,true);
      assert.equal(element('strategyListNext').disabled,true);
      if(!TOTAL) assert.match(element('strategyListPage').textContent,/暂无记录/);
      assert.deepEqual(requests.map(item=>new URL(item.url,'http://local').searchParams.get('page')),['1','2','2','1']);
    '''.replace('TOTAL',str(remaining_total)))


def test_history_paging_recovers_after_retention_shrinks_last_page() -> None:
    run(r'''
      let cleaned=false;
      const {controller,requests}=reader(url=>{
        if(!url.includes('/strategies/7/executions?')) return undefined;
        const page=Number(new URL(url,'http://local').searchParams.get('page'));
        return cleaned?historyPage(page,1,page===1?[9]:[]):historyPage(page,201,[9]);
      });
      element('strategySavedSelect').value='7';
      await click('strategyLoad');
      await click('strategyHistoryNext');
      cleaned=true;
      await click('strategyHistoryNext');
      assert.equal(controller.state.historyPage.page,1);
      assert.equal(element('strategyExecutionSelect').value,'9');
      assert.equal(element('strategyHistoryPrev').disabled,true);
      assert.equal(element('strategyHistoryNext').disabled,true);
      assert.deepEqual(requests.filter(item=>item.url.includes('/strategies/7/executions?'))
        .map(item=>new URL(item.url,'http://local').searchParams.get('page')),['1','2','3','1']);
    ''')


def test_page_correction_is_bounded_when_history_changes_twice() -> None:
    run(r'''
      let reads=0;
      const {controller}=reader(url=>{
        const page=Number(new URL(url,'http://local').searchParams.get('page'));
        reads++;
        return reads===1?savedPage(3,201,[7]):savedPage(page,reads===2?101:1,[]);
      });
      await controller.loadStrategies(null,3);
      const retained=controller.state.strategyPage;
      await controller.loadStrategies(null,3);
      assert.equal(reads,3);
      assert.equal(controller.state.strategyPage,retained);
      assert.match(status().textContent,/列表在读取期间变化/);
    ''')


def test_archived_confirmation_stays_selectable_when_list_refresh_fails() -> None:
    run(r'''
      const {controller}=reader((url,options)=>{
        if(url.endsWith('/archive')) return {...strategy(7),archived:true};
        if(url.includes('/strategies?')) throw new Error('刷新失败');
        return undefined;
      });
      loadEditor(controller,strategy(7));
      await click('strategyArchive');
      assert.equal(controller.state.strategy.archived,true);
      assert.equal(element('strategySavedSelect').value,'7');
      assert.match(element('strategySavedSelect').innerHTML,/已归档/);
      assert.equal(controller.state.strategies.length,0);
      assert.equal(element('strategyListNext').disabled,true);
      assert.match(status().textContent,/已归档.*同步未完成/);
    ''')


def test_bad_history_page_or_other_strategy_does_not_replace_loaded_history() -> None:
    run(r'''
      let corrupt=false;
      const {controller}=reader(url=>{
        if(corrupt&&url.includes('/executions?')) return {...historyPage(),items:[{...historyContext(),strategy_id:99}]};
        return undefined;
      });
      await openStrategy(controller);
      const retained=controller.state.historyPage;
      corrupt=true;
      await click('strategyHistoryRefresh');
      assert.equal(controller.state.historyPage,retained);
      assert.equal(element('strategyExecutionSelect').value,'9');
      assert.match(status().textContent,/不属于当前策略/);
    ''')
