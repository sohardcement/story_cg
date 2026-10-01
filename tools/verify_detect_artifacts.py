"""Check real calibration provenance and saved masks; no inference/remote calls."""
import hashlib
import json
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]

def main():
    manifest = json.loads((ROOT/'models/detect/manifest.json').read_text())
    for item in manifest['models']:
        path = ROOT/'models/detect'/item['filename']
        assert path.stat().st_size == item['size_bytes']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']
    results = json.loads((ROOT/'calibration/detect/results.json').read_text())
    assert [item['id'] for item in results['results']] == [f'D{i}' for i in range(1,15)]
    for item in results['results']:
        source = ROOT/item['image']
        assert hashlib.sha256(source.read_bytes()).hexdigest() == item['image_sha256']
        with Image.open(ROOT/item['overlay']) as overlay:
            assert list(overlay.size) == item['size']
        for person in item['persons']:
            with Image.open(ROOT/person['mask_path']) as mask:
                assert list(mask.size) == item['size'] and mask.mode == 'L'
                assert not any(mask.histogram()[1:255])
                measured = sum(mask.histogram()[1:])/(mask.width*mask.height)
                assert abs(measured-person['mask_area_ratio']) < 1e-9
    for filename, count in [('P9-01-cli-single.json',1),('P9-01-cli-multi.json',2)]:
        cli = json.loads((ROOT/'reports/assets'/filename).read_text())
        assert len(cli['results']) == count
        for item in cli['results']:
            assert (ROOT/item['overlay']).is_file()
            assert item['persons'][0]['mask_contours']
    print('Verified: two model hashes, 14 input hashes/overlays, original-size binary masks and measured areas, single/multiple CLI JSON.')

if __name__ == '__main__':
    main()
