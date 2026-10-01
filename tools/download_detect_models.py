"""Download pinned Bingsu/adetailer weights only into this project's models/."""
import hashlib
import json
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]

def main():
    api = 'https://huggingface.co/api/models/Bingsu/adetailer'
    with httpx.Client(follow_redirects=True, timeout=120, trust_env=False) as client:
        manifest_path = ROOT / 'models/detect/manifest.json'
        revision = (json.loads(manifest_path.read_text())['revision'] if manifest_path.exists()
                    else client.get(api).raise_for_status().json()['sha'])
        entries = []
        for name in ('face_yolov8s.pt', 'person_yolov8s-seg.pt'):
            url = f'https://huggingface.co/Bingsu/adetailer/resolve/{revision}/{name}'
            target = ROOT / 'models/detect' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix('.download')
            with client.stream('GET', url) as response:
                response.raise_for_status()
                with temp.open('wb') as output:
                    for chunk in response.iter_bytes():
                        output.write(chunk)
            temp.replace(target)
            entries.append({'filename': name, 'source_url': url, 'size_bytes': target.stat().st_size,
                            'sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
        manifest = {'repository': 'https://huggingface.co/Bingsu/adetailer', 'revision': revision,
                    'selection': 's variants requested by P9-01; higher model-card accuracy than n, smaller CPU cost than m.',
                    'models': entries}
        (ROOT / 'models/detect/manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
        print(json.dumps(manifest, indent=2))

if __name__ == '__main__':
    main()
