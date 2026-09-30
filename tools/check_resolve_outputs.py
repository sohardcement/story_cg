"""Check resolve artifacts against cached or fresh Danbooru source responses."""
import argparse
import hashlib
import json
from pathlib import Path

import httpx
from storycg.identity import USER_AGENT


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('root',type=Path)
    parser.add_argument('--live',action='store_true')
    args=parser.parse_args()
    seen={};report=[]
    with httpx.Client(headers={'User-Agent':USER_AGENT},trust_env=False,timeout=30) as client:
        for path in sorted(args.root.glob('*/identity.json')):
            identity=json.loads(path.read_text())
            assert identity['identity_tier']=='tier_pending_comfyui'
            assert identity['tier_evidence']['generation_count']==0
            directory=path.parent/'cache/danbooru'
            checked=0
            def payload(url):
                if url not in seen:
                    if args.live:
                        response=client.get(url);response.raise_for_status()
                        seen[url]=response.json()
                    else:
                        cache=directory/(hashlib.sha256(url.encode()).hexdigest()+'.json')
                        record=json.loads(cache.read_text())
                        assert record['url']==url
                        seen[url]=record['response']
                return seen[url]
            tag_groups = [identity.get(field, []) for field in
                          ('appearance_tags','default_outfit_tags','appearance_candidates','outfit_candidates')]
            tag_groups.extend(outfit['tags'] for outfit in identity.get('alternate_outfits', []))
            for items in tag_groups:
                for item in items:
                    for source in item['sources']:
                        assert source['url'] and source['method']
                        data=payload(source['url'])
                        if 'co-occurrence' in source['method']:
                            rows=[x for x in data['related_tags'] if x['tag']['name']==item['tag']]
                            assert rows and rows[0]['frequency']==source['frequency']
                        else:
                            assert source['evidence'] in data['body']
                        checked+=1
            if identity['status']=='locked':
                prefix = ['1girl', identity['character_tag']]
                if identity.get('work_tag'):
                    prefix.append(identity['work_tag'])
                tokens = identity['identity_block'].split(', ')
                assert tokens[:len(prefix)] == prefix
                assert tokens[len(prefix):] == [x['tag'] for x in identity['appearance_tags']]
                if identity.get('default_outfit_selection'):
                    selection = identity['default_outfit_selection']
                    base = selection.get('selected_base')
                    if base:
                        data = payload(selection['statistics_url'])
                        assert any(row['tag']['name']==base['tag'] and row['frequency']==base['frequency']
                                   for row in data['related_tags'])
            else:
                assert identity['status']=='needs_clarification'
                assert len(identity['candidates'])>1
            for candidate in identity['candidates']:
                entries=payload(candidate['source']['url'])
                assert any(x.get('value')==candidate['tag'] and x.get('category')==4 for x in entries)
                if candidate.get('work_source',{}):
                    work=candidate['work_source']
                    if 'evidence' in work:
                        assert work['evidence'] in payload(work['url'])['body']
            report.append({'name':identity['input_name'],'status':identity['status'],'tag_source_checks':checked})
    print(json.dumps({'live':args.live,'source_urls':len(seen),'results':report},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
