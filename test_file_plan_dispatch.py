import json
from test_activation_coordinate_gates import CoordinateGateTests, _install_build
from runtime import onboarding
from unittest.mock import patch
import os

class FilePlanDispatchTests(CoordinateGateTests):
    def test_generic_dispatch_and_consumer_binding(self):
        target = self.live_target()
        self.write_evidence(target)
        recipe = self.recipe()
        run = self.evidence_dir.parent
        (run/'recipes').mkdir(exist_ok=True)
        (run/'recipes/candidate.json').write_text(json.dumps({'recipe':recipe}))
        plan = onboarding.plan_from_recipe({**target, 'cwd':str(self.workspace)},recipe,'miss')
        self.assertEqual(plan['adapter'],'file_plan')
        self.assertEqual(plan['status'],'plan_pending_authorization')
        plan.update(workspace=str(self.workspace),investigation_run_dir=str(run),observed_compatibility=_install_build())
        # Existing fixture evidence folder may not be named evidence.
        if self.evidence_dir != run/'evidence':
            import shutil
            shutil.copytree(self.evidence_dir,run/'evidence')
        with patch.dict(os.environ,{'ASG_EXPERIENCE_DB':str(self.root/'experience.json')}):
            pending=onboarding.execute_install(plan,target,{'approved':False})
            self.assertEqual(pending['status'],'pending_authorization')
            result=onboarding.execute_install(plan,target,{'approved':True,'scope':'project','workspace':str(self.workspace)})
        self.assertEqual(result['status'],'installed',result)
        self.assertTrue(result['config_path'])
