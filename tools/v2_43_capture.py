"""V2-43 verification 1: record every ComfyUI submission the generation stage makes for one job.

Usage (from the v2 worktree root):
    PYTHONPATH=$PWD/src .venv/bin/python tools/v2_43_capture.py SOURCE_JOB_DIR WORK_DIR OUT_JSON

SOURCE_JOB_DIR is a read-only job from the main workspace. Only its job.json, identity.json,
storyboard.json, portrait.json and portrait/ are copied into WORK_DIR/<job name>, so the job name
(which seeds the candidate seeds) stays the same between runs. The ComfyUI side is a fake that records
each submitted workflow and returns a deterministic PNG; the vision side always returns a failed call.
No network and no real ComfyUI or vision service is used.
"""
import hashlib
import io
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from storycg import candidates, controls, review, tiers


class FakeComfy:
    def __init__(self):
        self.submissions = []
        self.prompts = {}
        self.images = {}

    def get_queue(self):
        return {'queue_running': [], 'queue_pending': []}

    def get_history(self, prompt_id):
        return {}

    def get_all_history(self):
        return {}

    def upload_image(self, path, **kwargs):
        return {'name': Path(path).name}

    def list_model_files(self, folder):
        return []

    def get_model_metadata(self, folder, filename):
        return {}

    def submit_prompt(self, workflow, client_id):
        prompt_id = f'fake-{len(self.submissions) + 1:04d}'
        text = json.dumps(workflow, ensure_ascii=False, sort_keys=True)
        # client_id is a random UUID made by the runner; it is not recorded, so runs can be compared.
        self.submissions.append({'prompt_id': prompt_id,
                                 'workflow_sha256': hashlib.sha256(text.encode()).hexdigest(),
                                 'workflow': workflow})
        self.prompts[prompt_id] = workflow
        return prompt_id

    def wait_for_history(self, prompt_id, interval_seconds, timeout_seconds):
        workflow = self.prompts[prompt_id]
        save_key = next(k for k, n in workflow.items() if n['class_type'] == 'SaveImage')
        prefix = workflow[save_key]['inputs']['filename_prefix']
        subfolder, _, base = prefix.rpartition('/')
        filename = f'{base}_00001_.png'
        width, height = 0, 0
        for node in workflow.values():
            if node['class_type'] == 'EmptyLatentImage':
                width, height = node['inputs']['width'], node['inputs']['height']
        self.images[filename] = (width, height, hashlib.sha256(prefix.encode()).digest()[:3])
        return {'status': {'status_str': 'success', 'completed': True},
                'outputs': {save_key: {'images': [{'filename': filename, 'subfolder': subfolder, 'type': 'output'}]}}}

    def download_image(self, reference):
        width, height, rgb = self.images[reference['filename']]
        buffer = Image.new('RGB', (width, height), tuple(rgb))
        out = io.BytesIO()
        buffer.save(out, format='PNG')
        return out.getvalue()

    def close(self):
        pass


def fake_vision_call(self, purpose, message_list, schema, paths=()):
    return {'status': 'fail', 'error': 'fake vision: no model service in verification', 'purpose': purpose}


def main(argv):
    source, work, out = Path(argv[1]), Path(argv[2]), Path(argv[3])
    root = work / source.name
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    for name in ('job.json', 'identity.json', 'storyboard.json', 'portrait.json'):
        shutil.copy2(source / name, root / name)
    shutil.copytree(source / 'portrait', root / 'portrait')
    job = json.loads((root / 'job.json').read_text(encoding='utf-8'))
    # The copy only: the time budget counts from created_at, and the source job is months old.
    job['created_at'] = datetime.now(timezone.utc).isoformat()
    (root / 'job.json').write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding='utf-8')
    identity = json.loads((root / 'identity.json').read_text(encoding='utf-8'))
    board = json.loads((root / 'storyboard.json').read_text(encoding='utf-8'))
    portrait = json.loads((root / 'portrait.json').read_text(encoding='utf-8'))
    # The generation stage of pipeline.run_job, minus the identity and portrait stages (not changed by V2-43).
    config = tiers.tier_generation_config(job['generation_config'], identity)
    config.pop('generation_reference', None)
    config['auto_retry_failed'] = False
    control = config['identity_control']
    client = FakeComfy()
    face_ref = (controls.portrait_reference(root, portrait, 'face', client, 'ipadapter', control['ipadapter_strength'])
                if control['mechanism'] == 'ipadapter' else None)
    review.VisionClient.call = fake_vision_call
    state = candidates.run_candidate_delivery(root, config, client, identity, board, face_ref)
    record = {'job': source.name, 'mechanism': control['mechanism'], 'ipadapter_strength': control.get('ipadapter_strength'),
              'submission_count': len(client.submissions),
              'submissions': [{k: v for k, v in s.items() if k != 'workflow'} for s in client.submissions],
              'workflows': [s['workflow'] for s in client.submissions],
              'status': state.get('status') if isinstance(state, dict) else None}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True) + '\n', encoding='utf-8')
    print(json.dumps({'job': source.name, 'submission_count': record['submission_count'],
                      'out': str(out)}, ensure_ascii=False))


if __name__ == '__main__':
    main(sys.argv)
