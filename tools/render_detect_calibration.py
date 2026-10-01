"""Render ALL saved current detections without rerunning or altering inference."""
import hashlib
import json
from PIL import Image, ImageDraw, ImageFont
from storycg.detect import ROOT, draw_overlay


def main():
    document = json.loads((ROOT/'calibration/detect/results.json').read_text())
    sheet = Image.new('RGB', (2560,2240), '#20242b')
    for index, saved in enumerate(document['results']):
        assert hashlib.sha256((ROOT/saved['image']).read_bytes()).hexdigest() == saved['image_sha256']
        result = {**saved, 'persons': []}
        for person in saved['persons']:
            with Image.open(ROOT/person['mask_path']) as mask:
                result['persons'].append({**person,'mask':mask.copy()})
        overlay = draw_overlay(ROOT/saved['image'], result)
        overlay.save(ROOT/saved['overlay'], quality=95)
        overlay.thumbnail((640,500))
        tile = Image.new('RGB', (640,560), '#20242b')
        tile.paste(overlay, ((640-overlay.width)//2,60+(500-overlay.height)//2))
        draw = ImageDraw.Draw(tile)
        draw.text((12,8), f'{saved["id"]}   Faces: {len(saved["faces"])}   Persons: {len(saved["persons"])}',
                  fill='white',font=ImageFont.load_default(size=24))
        draw.text((12,36), f'CPU {saved["timing_seconds"]["total"]:.3f}s',
                  fill='white',font=ImageFont.load_default(size=18))
        sheet.paste(tile, ((index%4)*640,(index//4)*560))
    sheet.save(ROOT/document['review'],quality=95)
    print('Rendered all 14 saved detections; no inference, no result edits.')

if __name__ == '__main__':
    main()
