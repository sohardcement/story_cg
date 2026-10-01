"""Posture membership derived solely from cached Danbooru group wiki bullets."""
import copy
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from storycg.story_rules import StoryRuleViolation
from storycg.tags import canonical_tag

CACHE = Path(__file__).resolve().parents[2] / 'cache/tag_groups'


def extract_tags(body):
    tags=set()
    for line in body.splitlines():
        if not re.match(r'^\*+\s+\[\[',line):continue
        for link in re.findall(r'\[\[([^\]]+)\]\]',line):
            tag=canonical_tag(link.split('|',1)[0])
            if ':' not in tag:tags.add(tag)
    return tags


@lru_cache(maxsize=1)
def posture_tags():
    raw=(CACHE/'posture.json').read_bytes()
    provenance=json.loads((CACHE/'posture.provenance.json').read_text())
    if hashlib.sha256(raw).hexdigest()!=provenance['sha256']:
        raise ValueError('posture source cache checksum mismatch')
    page=json.loads(raw)
    if page.get('title')!='tag_group:posture':raise ValueError('incorrect posture wiki source')
    tags=extract_tags(page['body'])
    if not tags:raise ValueError('posture source has no tag bullets')
    return frozenset(tags)


def tag_fields(board):
    for i,p in enumerate(board.get('props',[])):
        yield f'$.props[{i}].description_tags',p['description_tags']
    for i,e in enumerate(board['shots']):
        for block in ('scene','outfit'):
            yield f'$.shots[{i}].{block}.tags',e[block]['tags']
        yield f'$.shots[{i}].scene.negative_tags',e['scene']['negative_tags']
        for j,r in enumerate(e['shot']['key_props']):
            for field in ('relation_tags','location_tags'):
                yield f'$.shots[{i}].shot.key_props[{j}].{field}',r[field]


def validate_posture_blocks(board):
    vocabulary=posture_tags()
    for path,tags in tag_fields(board):
        found=[t for t in tags if canonical_tag(t) in vocabulary]
        if found:
            raise StoryRuleViolation('pose_field',[path],
                'posture tags belong only in shot.pose: '+', '.join(found),
                'remove these tags from this field; represent one instantaneous posture in pose')


def filtered_inputs(entry,props):
    board=copy.deepcopy({'shots':[entry],'props':props or []})
    vocabulary=posture_tags()
    for _,tags in tag_fields(board):tags[:]=[t for t in tags if canonical_tag(t) not in vocabulary]
    return board['shots'][0],board['props']
