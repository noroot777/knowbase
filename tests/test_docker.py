"""Container startup, data paths and Mac-only onboarding capability boundaries."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import deployment, pipeline_stages, wechat_setup
from kb.web import wechat_api

ROOT = Path(__file__).resolve().parents[1]


class ContainerAPITests(unittest.TestCase):
    def setUp(self):
        app = FastAPI(); app.include_router(wechat_api.router)
        self.client = TestClient(app, base_url='http://localhost:8787', client=('172.18.0.1', 40000))

    def test_bridge_peer_can_read_fixed_capabilities_without_host_diagnostics(self):
        with patch.object(wechat_api, 'in_container', return_value=True), \
             patch.object(wechat_setup, 'snapshot') as snapshot:
            response = self.client.get('/api/wechat/setup')
        self.assertEqual(200, response.status_code)
        self.assertEqual('host_required', response.json()['state'])
        self.assertFalse(response.json()['can_connect'])
        self.assertNotIn('confirmation_token', response.json())
        snapshot.assert_not_called()

    def test_container_cannot_launch_mac_acquisition_even_with_confirmation(self):
        with patch.object(wechat_api, 'in_container', return_value=True), \
             patch.object(wechat_setup, 'start') as start:
            for action in ('connect', 'acquire'):
                response = self.client.post('/api/wechat/setup',
                    json={'action': action, 'confirmed': True, 'confirmation_token': 'old-token'},
                    headers={'Origin': 'http://localhost:8787', 'X-KB-Wechat-Action': '1'})
                self.assertEqual(409, response.status_code)
            start.assert_not_called()

    def test_direct_call_also_cannot_prepare_or_acquire_in_container(self):
        with patch.object(deployment, 'in_container', return_value=True), \
             patch.object(wechat_setup, '_lock') as lock:
            with self.assertRaisesRegex(wechat_setup.runtime.SetupError, 'Docker'):
                wechat_setup.start()
            lock.assert_not_called()

    def test_non_container_still_rejects_remote_peers(self):
        with patch.object(wechat_api, 'in_container', return_value=False):
            self.assertEqual(403, self.client.get('/api/wechat/setup').status_code)


class ContainerRuntimeTests(unittest.TestCase):
    def test_exports_wiki_and_pipeline_use_the_same_mounted_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {**os.environ, 'KB_DATA_DIR': folder+'/data', 'KB_VAULT_ROOT': folder+'/vault',
                   'KB_VIDEOS_ROOT': folder+'/videos'}
            code = '''
import json,sys
from pathlib import Path
from kb import config,pipeline_stages
from scripts import gzh_projects_export as gzh,video_projects_export as video
from scripts.wiki import wikilib
sys.path.insert(0,'scripts/wiki')
import taxonomy
print(json.dumps([str(p) for p in [config.DATA_DIR,config.VAULT_ROOT,wikilib.DB,wikilib.VAULT,
gzh.STATE_PATH,gzh.OUT_ROOT,video.STATE_PATH,video.OUT_ROOT,video.VIDEOS_ROOT,
pipeline_stages.VIDEOS_DIR,taxonomy.CACHE_DIR]]))
'''
            result = subprocess.run([sys.executable, '-c', code], env=env, cwd=ROOT,
                                    capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            paths = json.loads(result.stdout)
            self.assertTrue(all(p.startswith(folder+'/') for p in paths), paths)
            self.assertEqual(paths[1], paths[3])
            self.assertEqual(paths[8], paths[9])

    def test_pipeline_child_uses_installed_python_without_runtime_resolution(self):
        with patch.object(pipeline_stages.subprocess, 'run') as run:
            pipeline_stages._run_export_script('gzh_projects_export.py', 'scan', timeout_min=1)
        self.assertEqual(sys.executable, run.call_args.args[0][0])
        self.assertNotIn('uv', run.call_args.args[0])

    def test_entrypoint_initializes_new_volume_and_preserves_it_on_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {**os.environ, 'PYTHONPATH': str(ROOT), 'KB_DATA_DIR': folder+'/data',
                   'KB_VAULT_ROOT': folder+'/vault', 'KB_COLLECTOR_DIR': folder+'/collector',
                   'HF_HOME': folder+'/cache'}
            command = [sys.executable, str(ROOT/'docker/entrypoint.py'), sys.executable, '-c',
                       'from kb import store; c=store.connect(); print(c.execute("SELECT COUNT(*) FROM documents").fetchone()[0])']
            first = subprocess.run(command, env=env, cwd=ROOT, capture_output=True, text=True, check=True)
            self.assertEqual('0', first.stdout.strip())
            marker = Path(folder)/'data/keep.txt'; marker.write_text('persist')
            subprocess.run(command, env=env, cwd=ROOT, capture_output=True, text=True, check=True)
            self.assertEqual('persist', marker.read_text())

    def test_cpu_export_keeps_torch_pin_and_removes_cuda_requirements(self):
        spec = importlib.util.spec_from_file_location('export_requirements', ROOT/'docker/export_requirements.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        result = module.without_cuda('torch==2.12.1\nnvidia-cublas==13.0\ncuda-toolkit==13.0\ntriton==3.7\ntransformers==5.0\n')
        self.assertEqual('torch==2.12.1\ntransformers==5.0\n', result)


if __name__ == '__main__':
    unittest.main()
