"""P9-01 supplied read-only samples; serial local detector calibration."""
import hashlib
import json
from pathlib import Path
import time
from PIL import Image, ImageDraw, ImageFont
from storycg.detect import ROOT, detect_image, draw_overlay, json_result, load_detect_config

SAMPLES = [
('D1', 'jobs/cg-76d2eec57dfc/storycg_cg-76d2eec57dfc_shot01_a001.png', '两个主角（趴桌睡 + 站立），人物应计 2'),
('D2', 'jobs/cg-76d2eec57dfc/storycg_cg-76d2eec57dfc_shot02_a001.png', '单人正面'),
('D3', 'jobs/cg-76d2eec57dfc/storycg_cg-76d2eec57dfc_shot03_a002.png', '本人 + 窗玻璃倒影'),
('D4', 'runs/p3_02_b_ipadapter/storycg_p3_02_b_ipadapter_shot01_a001.png', '远景背影，人物 1、无脸'),
('D5', 'runs/p3_02_b_ipadapter/storycg_p3_02_b_ipadapter_shot03_a001.png', '背影奔跑，人物 1、无脸'),
('D6', 'runs/p3_02_b_ipadapter/storycg_p3_02_b_ipadapter_shot04_a001.png', '背影 + 画外他人的手'),
('D7', 'runs/p3_02_a_ipadapter/storycg_p3_02_a_ipadapter_shot01_a001.png', '远景小脸（约 100px）'),
('D8', 'runs/p1_02_frieren_20260930/storycg_p1_02_frieren_20260930_shot04_a001.png', '主角 + 背景路人（背影男性）'),
('D9', 'runs/p5_02_a_r1/storycg_p5_02_a_r1_shot04_a001.png', '主角 + 画外陌生人的手'),
('D10', 'jobs/cg-fc625e111558/storycg_cg-fc625e111558_shot02_a001.png', '本人 + 镜中倒影'),
('D11', 'jobs/cg-fc625e111558/storycg_cg-fc625e111558_shot04_a001.png', '背对镜子 + 镜中倒影'),
('D12', 'runs/p5_02_b_r1/storycg_p5_02_b_r1_shot03_a002.png', '单人奔跑正面'),
('D13', 'calibration/samples/C2.png', '单人中景，有脸'),
('D14', 'calibration/samples/E1a.png', '单人中景（换角色样本）'),
]


def main():
    started = time.perf_counter()
    output_dir = ROOT / 'calibration/detect'
    output_dir.mkdir(parents=True, exist_ok=True)
    config = load_detect_config()
    results = []
    tiles = []
    for label, source, expected in SAMPLES:
        path = ROOT / source
        result = detect_image(path, config=config)
        output = json_result(result, output_dir / label / 'masks')
        for person in output['persons']:
            if 'mask_path' in person:
                person['mask_path'] = str(Path(person['mask_path']).relative_to(ROOT))
        overlay_path = output_dir / f'{label}-overlay.jpg'
        overlay = draw_overlay(path, result)
        overlay.save(overlay_path, quality=95)
        entry = {'id': label, 'image': source, 'image_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                 'supervisor_expectation': expected, 'overlay': str(overlay_path.relative_to(ROOT)), **output}
        results.append(entry)
        print(f'{label}: faces={len(result["faces"])} persons={len(result["persons"])} seconds={result["timing_seconds"]["total"]:.3f}', flush=True)
        tile = Image.new('RGB', (640, 560), '#20242b')
        overlay.thumbnail((640, 500))
        tile.paste(overlay, ((640-overlay.width)//2, 60+(500-overlay.height)//2))
        draw = ImageDraw.Draw(tile)
        font = ImageFont.load_default(size=24)
        draw.text((12,8), f'{label}   Faces: {len(result["faces"])}   Persons: {len(result["persons"])}', fill='white', font=font)
        draw.text((12,36), f'CPU {result["timing_seconds"]["total"]:.3f}s', fill='white', font=ImageFont.load_default(size=18))
        tiles.append(tile)
    sheet = Image.new('RGB', (640*4, 560*4), '#20242b')
    for index,tile in enumerate(tiles):
        sheet.paste(tile, ((index%4)*640, (index//4)*560))
    review = ROOT / 'review/P9-01-detect.jpg'
    review.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(review, quality=95)
    document = {'config': config, 'model_manifest': json.loads((ROOT/'models/detect/manifest.json').read_text()),
                'notes': 'No semantic subject/reflection filtering. area_ratio=bbox area; mask_area_ratio=segmentation pixel area.',
                'results': results, 'elapsed_seconds': time.perf_counter()-started, 'review': str(review.relative_to(ROOT))}
    (output_dir / 'results.json').write_text(json.dumps(document, ensure_ascii=False, indent=2)+'\n')

if __name__ == '__main__':
    main()
