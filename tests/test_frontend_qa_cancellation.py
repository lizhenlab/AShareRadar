"""Question controls recover when their load owner aborts but keeps the DOM."""
from __future__ import annotations

import pytest

from tests.test_frontend_research_panels import _run_node_script


@pytest.mark.parametrize("requested_symbol", ["600519.SH", "000001.SZ"])
def test_failed_app_refresh_releases_question_form_and_next_question_really_succeeds(requested_symbol):
    _run_node_script(f'const requestedSymbol={requested_symbol!r};' + r'''
      const { createAppHarness } = await import("./tests/frontend_app_flow_helpers.mjs");
      const { renderAiDashboard } = await import("./static/js/research-qa-reports.js");
      const { createRequestScope } = await import("./static/js/api.js");
      const {__appTest,element}=await createAppHarness({canvasContext:null});
      const state=__appTest.state;
      state.symbol="600519.SH"; state.loadSeq=1; state.loadRequest=createRequestScope();
      state.lastAnalysis={quote:{code:"600519",market:"SH",name:"贵州茅台",price:100,timestamp:"2026-09-09 10:00:00"}};
      let asks=0,firstSignal,secondSignal,rendered="";
      globalThis.fetch=async(url,options={})=>{
        if(String(url)==="/api/stock/ask") {
          asks+=1;
          if(asks===1) {firstSignal=options.signal;return new Promise(()=>{});}
          secondSignal=options.signal; return new Response(JSON.stringify({question:"风险在哪里？",answer:"恢复后的当前回答",confidence:70}));
        }
        if(String(url).startsWith("/api/stock/workbench")) return new Response(JSON.stringify({detail:"synthetic unavailable"}),{status:503});
        return new Response(JSON.stringify([]));
      };
      renderAiDashboard(workbench(),state);
      const form=element("aiQuestionForm"),button=element("aiQuestionForm-button");
      form.closest=()=>({querySelector:()=>null}); form.insertAdjacentHTML=(_where,html)=>{rendered=html;};
      element("aiQuestionInput").value="风险在哪里？";
      const pending=form.listeners.submit({preventDefault(){},currentTarget:form});
      if(!button.disabled) throw new Error("question never became busy");
      __appTest.setActiveSymbol(requestedSymbol);
      const loaded=await __appTest.loadAll(); await pending;
      if(loaded || !firstSignal.aborted || state.symbol!=="600519.SH" || element("aiQuestionForm")!==form) throw new Error("probe did not retain failed-refresh DOM");
      if(button.disabled || button.textContent!=="问一下" || form.getAttribute("aria-busy")!=="false") throw new Error("retained form is permanently busy after cancellation");
      await form.listeners.submit({preventDefault(){},currentTarget:form});
      if(asks!==2 || secondSignal.aborted || !rendered.includes("恢复后的当前回答")) throw new Error("next question did not really recover");
    ''')


def test_aborted_old_question_cannot_release_a_new_forms_busy_state_or_write_its_answer():
    _run_node_script(r'''
      const {renderResearch}=await import("./static/js/research-panels.js");
      const dom=installResearchPanelDom(),state={symbol:"600519.SH",loadSeq:1};
      const old=deferredReply(),next=deferredReply();let calls=0;
      globalThis.fetch=async()=>++calls===1?old.promise:next.promise;
      renderResearch(workbench(),state); dom.input().value="第一问";
      const first=dom.form().listener.handler({preventDefault(){},currentTarget:dom.form()});
      state.loadSeq=2; renderResearch(workbench(),state); dom.input().value="第二问";
      const second=dom.form().listener.handler({preventDefault(){},currentTarget:dom.form()});
      await first;
      if(!dom.button().disabled || dom.form().getAttribute("aria-busy")!=="true") throw new Error("old finally released new form");
      old.resolve(answerResponse("旧答案")); await Promise.resolve();
      if(dom.answerHtml().includes("旧答案")) throw new Error("old answer replaced new owner");
      next.resolve(answerResponse("新答案"));await second;
      if(dom.button().disabled || !dom.answerHtml().includes("新答案")) throw new Error("new form failed to recover normally");
    ''')
