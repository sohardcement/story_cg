"""V2-25: fetch the third-party WAI character list (MIT) into data/wai/ and write its manifest.

Downloads are made with trust_env=False (the environment has a SOCKS proxy). Re-running re-downloads and
rewrites the manifest; the sha256 in it always describes the bytes on disk.
Usage: PYTHONPATH=$PWD/src .venv/bin/python tools/v2_25_fetch_wai.py
"""
import datetime
import hashlib
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data' / 'wai'
REPO = 'https://github.com/mirabarukaso/character_select_stand_alone_app'
RAW = 'https://raw.githubusercontent.com/mirabarukaso/character_select_stand_alone_app/main'
FILES = {
    'csv': ('data/waiIllustriousSDXL_v160_characters.csv', 'waiIllustriousSDXL_v160_characters.csv'),
    'license': ('LICENSE', 'LICENSE.upstream'),
}


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    entries = {}
    with httpx.Client(trust_env=False, follow_redirects=True, timeout=60) as client:
        for key, (remote, local) in FILES.items():
            response = client.get(f'{RAW}/{remote}')
            response.raise_for_status()
            body = response.content
            (DATA / local).write_bytes(body)
            entries[key] = {'url': f'{RAW}/{remote}', 'file': f'data/wai/{local}', 'bytes': len(body),
                            'sha256': hashlib.sha256(body).hexdigest()}
    csv_bytes = (DATA / FILES['csv'][1]).read_bytes()
    manifest = {
        'source_repo': REPO,
        'license': 'MIT (upstream LICENSE file is saved as data/wai/LICENSE.upstream; see its sha256)',
        'downloaded_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
        'wai_version': 'WAI Illustrious v16 (the list is named v160)',
        'project_checkpoint_note': '项目底模是 WAI v17，比清单晚一版；清单只用来判定角色是否可以只靠 tag 画出来',
        'rows': len([line for line in csv_bytes.decode('utf-8-sig').splitlines() if line.strip()]),
        'csv': {'file': entries['csv']['file'], 'url': entries['csv']['url'], 'sha256': entries['csv']['sha256'],
                'bytes': entries['csv']['bytes']},
        'license_file': {'file': entries['license']['file'], 'url': entries['license']['url'],
                         'sha256': entries['license']['sha256'], 'bytes': entries['license']['bytes']},
    }
    (DATA / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
