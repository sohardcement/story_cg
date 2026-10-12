"""V2-50 (D87): face refinement of small faces in far shots, before a candidate is audited.

A candidate whose face is too small is refined once: the face is cropped as a square (crop_scale x its larger
side, centred on the face, clamped to the image), resized to side x side and re-drawn by img2img with the
candidate's own workflow (seed, IPAdapter, negative, sampler) and a close-up framing prompt. The result is
resized back and pasted into the candidate with an elliptical mask around the face only.

Geometry, prompt and mask follow the face-ab experiment (verify/v2/face-ab/run_face.py, repaste.py).
The refinement is a separate ComfyUI submission (attempt with shot_id 'shotNN_face', kind 'face_refine')
that goes through runner._process, so it is reconciled after an interruption and never reposted.
"""
import copy
import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from storycg import runner
from storycg.budget import BudgetExceeded
from storycg.controls import upload_reference
from storycg.detect import detect_faces

# Terminal states: the candidate keeps its final image (refined) or its original one (the rest).
FINAL_STATES = ('refined', 'no_face', 'not_needed', 'skipped_budget', 'failed')
REASONS = {'skipped_budget': '出图额度不够', 'failed': '精修出图失败'}


def settings(config):
    """The face_refine section of the job's config; None for old jobs, which then behave as before."""
    return (config or {}).get('face_refine')


def pick_face(faces):
    return max(faces, key=lambda face: face['confidence']) if faces else None


def long_side(bbox):
    x0, y0, x1, y1 = bbox
    return max(x1 - x0, y1 - y0)


def crop_box(size, bbox, scale):
    width, height = size
    x0, y0, x1, y1 = bbox
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    side = min(long_side(bbox) * scale, width, height)
    left = min(max(0, cx - side / 2), width - side)
    top = min(max(0, cy - side / 2), height - side)
    return tuple(int(round(v)) for v in (left, top, left + side, top + side))


def _node_of(workflow, kind):
    return next(key for key, node in workflow.items() if node['class_type'] == kind)


def refine_workflow(source, uploaded, positive_prefix, closeup_tags, denoise, prefix):
    """The candidate's workflow with the crop as the latent and the close-up prompt; nothing else changes."""
    workflow = copy.deepcopy(source)
    sampler = workflow[_node_of(workflow, 'KSampler')]['inputs']
    workflow[sampler['positive'][0]]['inputs']['text'] = f'{positive_prefix}, {closeup_tags}'
    checkpoint = _node_of(workflow, 'CheckpointLoaderSimple')
    workflow.pop(_node_of(workflow, 'EmptyLatentImage'))
    workflow['60'] = {'class_type': 'LoadImage', 'inputs': {'image': uploaded}}
    workflow['61'] = {'class_type': 'VAEEncode', 'inputs': {'pixels': ['60', 0], 'vae': [checkpoint, 2]}}
    sampler['latent_image'] = ['61', 0]
    sampler['denoise'] = denoise
    workflow[_node_of(workflow, 'SaveImage')]['inputs']['filename_prefix'] = prefix
    return workflow


def tight_paste(original, refined, box, bbox, mask_scale, feather):
    """The refined crop, resized back to the crop box, pasted through an ellipse around the face only."""
    size = box[2] - box[0]
    patch = refined.resize((size, size), Image.LANCZOS)
    x0, y0, x1, y1 = bbox
    cx, cy = (x0 + x1) / 2 - box[0], (y0 + y1) / 2 - box[1]
    rx, ry = (x1 - x0) * mask_scale / 2, (y1 - y0) * mask_scale / 2
    blur = max(x1 - x0, y1 - y0) * feather
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).ellipse((cx - rx + blur, cy - ry + blur, cx + rx - blur, cy + ry - blur), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(blur))
    out = original.copy()
    out.paste(patch, box[:2], mask)
    return out


def summary(face):
    """What candidates.json, manifest.json and result.md carry for one candidate."""
    record = {'status': face['status'], 'face_long_side': face.get('face_long_side')}
    if face['status'] == 'refined':
        record['original_image'] = face['original_image']['path']
    return record


def _candidate_attempt(state, shot_id, candidate):
    return [a for a in state['attempts'] if a['shot_id'] == shot_id and a.get('candidate') == candidate][-1]


def _refine_attempt(state, shot_id, candidate):
    refines = {'shot_id': shot_id, 'candidate': candidate}
    return next((a for a in state['attempts'] if a.get('kind') == 'face_refine' and a.get('refines') == refines), None)


def _inspect(root, attempt, cfg):
    faces = detect_faces(root / attempt['image']['path'])
    face = pick_face(faces)
    if face is None:
        return {'status': 'no_face', 'face_long_side': None}
    side = long_side(face['bbox'])
    if side >= cfg['max_face_px']:
        return {'status': 'not_needed', 'face_long_side': round(side, 1)}
    return {'status': 'pending', 'face_long_side': round(side, 1), 'face_bbox': list(face['bbox']),
            'confidence': face['confidence']}


def _start(root, attempt, cfg, parts, client, budget):
    """Budget check, crop and upload. Leaves the face record 'submitted' with everything a resume needs."""
    face = attempt['face_refine']
    try:
        budget.check(images=1)
    except BudgetExceeded:
        face['status'] = 'skipped_budget'
        return
    source = root / attempt['image']['path']
    box = crop_box((attempt['image']['width'], attempt['image']['height']), face['face_bbox'], cfg['crop_scale'])
    try:
        crop = Image.open(source).convert('RGB').crop(box).resize((cfg['side'], cfg['side']), Image.LANCZOS)
        crop_path = root / 'face_refine' / f'{Path(source).stem}_crop.png'
        crop_path.parent.mkdir(parents=True, exist_ok=True)
        crop.save(crop_path)
        uploaded = upload_reference(root, crop_path, client)
    except Exception as exc:  # the crop or upload failed: keep the original image
        face.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        return
    face.update(status='submitted', crop_box=list(box), denoise=cfg['denoise'], uploaded_image=uploaded,
                closeup_positive_tags=parts['closeup_positive_tags'])


def _submit(root, state, attempt, client):
    """Creates the refinement attempt (once) and runs it through the runner's reconciliation."""
    face = attempt['face_refine']
    refine = _refine_attempt(state, attempt['shot_id'], attempt['candidate'])
    created = refine is None
    if created:
        refines = {'shot_id': attempt['shot_id'], 'candidate': attempt['candidate']}
        try:
            refine = runner._new_attempt(
                root, state, f"{attempt['shot_id']}_face", face['closeup_positive_tags'], attempt['framing'],
                seed=attempt['seed'], generation_reference=attempt.get('reference'),
                workflow_for=lambda prefix: refine_workflow(
                    attempt['workflow'], face['uploaded_image'], state['config']['positive_prefix'],
                    face['closeup_positive_tags'], face['denoise'], prefix),
                extra={'kind': 'face_refine', 'refines': refines})
        except runner.JobBudgetExhausted:
            face['status'] = 'skipped_budget'
            return
    try:
        if refine['status'] not in ('completed', 'failed'):
            runner._process(root, state, refine, client, recovering=not created)
    except Exception as exc:  # the refinement submission failed: keep the original image
        face.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        return
    if refine['status'] == 'completed':
        _compose(root, attempt, face, refine, state['config'])
    else:
        face.update(status='failed', error=refine.get('error') or refine['status'])


def _compose(root, attempt, face, refine, config):
    """The composite replaces the candidate's image; the original stays in face_refine."""
    cfg = settings(config)
    original = attempt['image']
    refined = Image.open(root / refine['image']['path']).convert('RGB')
    source = Image.open(root / original['path']).convert('RGB')
    out_path = root / f"{Path(original['path']).stem}_face.png"
    tight_paste(source, refined, tuple(face['crop_box']), face['face_bbox'], cfg['mask_scale'], cfg['feather']).save(out_path)
    with Image.open(out_path) as image:
        width, height = image.size
    attempt['image'] = {'path': out_path.name, 'sha256': hashlib.sha256(out_path.read_bytes()).hexdigest(),
                        'width': width, 'height': height}
    face.update(status='refined', original_image=original, refine_attempt_number=refine['attempt_number'])


def refine_candidate(job_dir, shot_id, candidate, *, parts, client, budget):
    """Refine one completed candidate in place (once) and return its face record; None for jobs without the section.

    Safe to call again after an interruption: a finished record is returned as it is, a submitted one is
    reconciled, and a downloaded refinement whose composite was not written yet is composed now.
    """
    root = Path(job_dir)
    with runner._job_lock(root):
        state = runner.load_state(root)
        cfg = settings(state['config'])
        if cfg is None:
            return None
        attempt = _candidate_attempt(state, shot_id, candidate)
        face = attempt.get('face_refine')
        if face is None:
            face = attempt['face_refine'] = _inspect(root, attempt, cfg)
            runner._save(root, state)
        if face['status'] == 'pending':
            _start(root, attempt, cfg, parts, client, budget)
            runner._save(root, state)
        if face['status'] == 'submitted':
            _submit(root, state, attempt, client)
            runner._save(root, state)
        return face
