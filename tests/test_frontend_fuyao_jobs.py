from __future__ import annotations

from tests.test_frontend_fuyao import _HARNESS, _node


_JOB = """
  const request={kind:'financials',symbols:['600519.SH','000001.SZ'],index_symbols:[],period:'annual',limit:4,report:'2025-4'};
  const job={id:'original',kind:'financials',status:'running',request,completed:1,total:2,completed_symbols:['600519.SH'],created_at:'2026-09-10'};
  const settle=()=>new Promise(resolve=>setImmediate(resolve));
"""


def test_task_contracts_use_authoritative_activity_and_structured_remaining_symbols() -> None:
    _node(_HARNESS + _JOB + """
      const {activeFuyaoJob,canRetryFuyaoJob,verifiedFuyaoJob}=await import('./static/js/fuyao-contracts.js');
      assert.equal(activeFuyaoJob({jobs:[],active_jobs:[]},job),null);
      assert.equal(activeFuyaoJob({jobs:[],active_jobs:['original']},{...job,status:'cancelling'}).status,'cancelling');
      assert.equal(canRetryFuyaoJob({...job,status:'degraded'}),true);
      assert.equal(canRetryFuyaoJob({...job,status:'degraded',completed_symbols:request.symbols}),false);
      assert.equal(canRetryFuyaoJob({...job,status:'cancelled',request:null}),false);
      assert.equal(canRetryFuyaoJob({...job,status:'cancelling'}),false);
      assert.equal(canRetryFuyaoJob({...job,kind:'history_full',status:'failed'}),true);
      assert.throws(()=>verifiedFuyaoJob({...job,id:'wrong'},'original'));
    """)


def test_missing_running_receipt_is_reconciled_by_id_before_releasing_controls() -> None:
    _node(_HARNESS + _JOB + """
      const reads=[];
      const owner=createFuyaoController({documentTarget:doc,getSymbol:()=>current,fetchJson:async url=>{
        reads.push(url);
        if(url.endsWith('/status'))return {...status,active_jobs:[],jobs:[]};
        if(url.endsWith('/jobs/original'))return {...job,status:'completed',completed:2,completed_symbols:request.symbols};
        return url.endsWith('/market')?{}:observation(current);
      }});
      owner.status={...status,jobs:[job],active_jobs:['original']};owner.accepted=job;
      await owner.readStatus();
      assert.ok(reads.includes('/api/fuyao/jobs/original'));
      assert.equal(owner.accepted.status,'completed');
      assert.ok(nodes.get('fuyaoJobs').innerHTML.includes('已完成'));
      owner.destroy();
    """)


def test_a_missing_task_or_failed_lookup_has_explicit_recovery_without_submission() -> None:
    _node(_HARNESS + _JOB + """
      let errorStatus=503;
      const owner=createFuyaoController({documentTarget:doc,fetchJson:async url=>{
        if(url.endsWith('/status'))return {...status,jobs:[],active_jobs:[]};
        if(url.endsWith('/jobs/original'))throw Object.assign(new Error('不可用'),{status:errorStatus});
        return url.endsWith('/market')?{}:observation(current);
      }});
      owner.status={...status,jobs:[job],active_jobs:['original']};owner.accepted=job;
      await owner.readStatus();
      assert.equal(owner.accepted.status,'running');
      assert.ok(nodes.get('fuyaoStatusSummary').textContent.includes('读取失败'));
      errorStatus=404;
      await owner.readStatus();
      assert.equal(owner.accepted,null);
      owner.destroy();
    """)


def test_a_status_read_started_before_submit_cannot_overwrite_the_new_receipt() -> None:
    _node(_HARNESS + _JOB + """
      const statuses=[];let releasePost;
      const owner=createFuyaoController({documentTarget:doc,fetchJson:async(url,options={})=>{
        if(options.method==='POST')return await new Promise(resolve=>{releasePost=resolve;});
        if(url.endsWith('/status'))return await new Promise(resolve=>statuses.push(resolve));
        throw new Error('unexpected read');
      }});
      owner.status={...status,active_jobs:[]};
      const old=owner.readStatus();
      const post=owner.submit('financials',true);
      releasePost(job);await post;
      statuses[0]({...status,jobs:[],active_jobs:[]});await old;
      assert.equal(owner.accepted.id,'original');
      assert.equal(owner.status.jobs[0].status,'running');
      assert.equal(statuses.length,2);
      statuses[1]({...status,jobs:[job],active_jobs:['original']});await settle();
      owner.destroy();
    """)


def test_cancel_survives_navigation_and_retry_only_posts_the_parent_identity() -> None:
    _node(_HARNESS + _JOB + """
      let releasePost;let serverJob=job;const calls=[];
      const owner=createFuyaoController({documentTarget:doc,fetchJson:async(url,options={})=>{
        calls.push([url,options]);
        if(options.method==='POST')return await new Promise(resolve=>{releasePost=resolve;});
        if(url.endsWith('/status'))return {...status,jobs:[serverJob],active_jobs:serverJob.status==='cancelling'?['original']:[]};
        return url.endsWith('/market')?{}:observation(current);
      }});
      owner.status={...status,jobs:[job],active_jobs:['original']};owner.accepted=job;
      const cancel=owner.jobs.action('cancel','original');
      owner.setWorkspace('quote');
      assert.equal(calls[0][1].signal,undefined);
      serverJob={...job,status:'cancelling'};releasePost(serverJob);await cancel;await settle();
      assert.ok(nodes.get('fuyaoJobs').innerHTML.includes('正在停止'));
      await owner.submit('financials');
      assert.equal(calls.filter(([,options])=>options.method==='POST').length,1);
      serverJob={...job,status:'cancelled'};await owner.readStatus();
      const retry=owner.jobs.action('retry','original');
      assert.equal(calls.at(-1)[0],'/api/fuyao/jobs/original/retry');
      assert.equal(calls.at(-1)[1].body,undefined);
      serverJob={...job,id:'child',parent_job_id:'original',request:{...request,symbols:['000001.SZ']},completed:0,total:1,completed_symbols:[]};
      releasePost(serverJob);await retry;await settle();
      assert.equal(owner.accepted.id,'child');
      assert.ok(nodes.get('fuyaoJobDetail').innerHTML.includes('补做来源任务 original'));
      owner.destroy();
    """)


def test_detail_reads_are_explicit_identity_checked_and_released_on_navigation() -> None:
    _node(_HARNESS + _JOB + """
      let resolveRead;const calls=[];
      const owner=createFuyaoController({documentTarget:doc,fetchJson:async(url,options)=>{
        calls.push([url,options]);return await new Promise(resolve=>{resolveRead=resolve;});
      }});
      owner.bind();assert.equal(calls.length,0);
      const pending=owner.jobs.read('original');
      owner.setWorkspace('finance');
      assert.equal(calls[0][1].signal.aborted,true);
      resolveRead(job);await pending;
      assert.equal(owner.jobs.job,null);
      const second=owner.jobs.read('original');resolveRead({...job,id:'wrong'});await second;
      assert.ok(nodes.get('fuyaoJobDetail').innerHTML.includes('身份或状态异常'));
      const third=owner.jobs.read('original');resolveRead({...job,status:'degraded'});await third;
      const html=nodes.get('fuyaoJobDetail').innerHTML;
      for(const text of ['600519.SH','000001.SZ','2025-4','已保存股票'])assert.ok(html.includes(text));
      owner.destroy();
    """)


def test_task_views_keep_unknown_totals_legacy_parameters_and_untrusted_text_explicit() -> None:
    _node(_HARNESS + _JOB + """
      const {renderFuyaoJobs,renderFuyaoJobDetail}=await import('./static/js/fuyao-job-view.js');
      const target={innerHTML:''};
      renderFuyaoJobs(target,{jobs:[{...job,request:null,status:'failed',progress:{stage:'downloading_daily',current:1024,total:null,unit:'bytes'}}]});
      for(const text of ['下载日线','1,024','总量未知','旧任务未保存输入参数'])assert.ok(target.innerHTML.includes(text));
      assert.ok(!target.innerHTML.includes('%'));
      renderFuyaoJobDetail(target,{...job,request:{...request,symbols:['<script>x</script>']}},{id:job.id});
      assert.ok(!target.innerHTML.includes('<script>'));assert.ok(target.innerHTML.includes('&lt;script&gt;'));
    """)
