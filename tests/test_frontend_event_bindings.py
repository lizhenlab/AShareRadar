from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "module,binder,target,event_type",
    [
        ("advice-review-events", "bindAdviceReviewEvents", "reviewPlanLoadMore", "click"),
        ("stock-note-alert-events", "bindStockNoteAlertEvents", "noteForm", "submit"),
    ],
)
def test_feature_binding_owns_one_listener_group_per_root_and_can_rebind(
    module: str, binder: str, target: str, event_type: str,
) -> None:
    script = f'''
      import {{ {binder} as bind }} from "./static/js/{module}.js";
      const targets = new Map();
      const root = {{ getElementById(id) {{
        if (!targets.has(id)) targets.set(id, new EventTarget());
        return targets.get(id);
      }} }};
      let calls = 0;
      const options = {{ root, currentWorkbenchMutationOptions() {{ calls += 1; return null; }} }};
      const dispose = bind(options);
      const duplicate = bind({{ root, currentWorkbenchMutationOptions() {{ throw new Error("duplicate owner ran"); }} }});
      if (duplicate !== dispose) throw new Error("same root acquired two binding owners");
      const fire = () => root.getElementById("{target}").dispatchEvent(new Event("{event_type}", {{ cancelable: true }}));
      fire();
      if (calls !== 1) throw new Error(`expected one handler, got ${{calls}}`);
      dispose();
      dispose();
      fire();
      if (calls !== 1) throw new Error("disposed listeners still fired");
      const nextDispose = bind(options);
      dispose();
      fire();
      if (calls !== 2) throw new Error("old disposal detached the new binding owner");
      nextDispose();
    '''
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
