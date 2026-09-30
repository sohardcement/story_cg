"""Conservative, configured identity extraction; images never provide identity evidence."""
import copy
import json
import re
from pathlib import Path

RULES_PATH = Path(__file__).resolve().parents[2] / 'config' / 'identity_rules.json'


def load_rules(path=None):
    return json.loads(Path(path or RULES_PATH).read_text(encoding='utf-8'))


def category_for(tag, rules):
    for category in rules['categories']:
        if re.search(category['pattern'], tag):
            return category
    return None


def _group(tag, category):
    if category.get('exclusive_by_suffix'):
        # Color variants conflict; generic parent tags remain compatible.
        pieces = tag.split('_', 1)
        colors = {'black','white','red','blue','green','yellow','purple','pink','gold','silver','brown','grey','gray','orange','aqua'}
        return category['name'] + ':' + pieces[1] if pieces[0] in colors and len(pieces)>1 else None
    return category['name'] if category.get('exclusive') else None


def wiki_descriptors(body, url, rules):
    """Linked tags in descriptive sentences only; preserve prose and variant evidence."""
    found = {}
    for paragraph in re.split(r'\r?\n\s*\r?\n', body):
        for sentence in re.split(r'(?<=[.!?])\s+', paragraph):
            if not re.search(r'\b(?:has|have|with|wears?|appearance|features|dressed|portrayed|depicted|consists|clothing|costume)\b',sentence,re.I):
                continue
            variant = bool(re.search(rules['wiki_variant_markers'],sentence,re.I))
            for match in re.finditer(r'\[\[([^]|]+)(?:\|[^]]*)?\]\]',sentence):
                tag=re.sub(r'\s+','_',match.group(1).strip().lower())
                cat=category_for(tag,rules)
                if not cat:
                    continue
                item={'tag':tag,'category':cat['name'],'variant':variant,
                      'source':{'url':url,'method':'linked tag in descriptive wiki sentence',
                                'frequency':None,'evidence':sentence}}
                found.setdefault(tag,item)
    return list(found.values())


def enrich_identity(identity, related, url, rules):
    if not url:
        raise ValueError('statistics requires source URL')
    result=copy.deepcopy(identity)
    merged={}
    for key in ('appearance_tags','default_outfit_tags'):
        for item in result.get(key,[]):
            if not item.get('source',{}).get('url') or not item.get('source',{}).get('method'):
                raise ValueError('identity tag requires source URL and method')
            cat=category_for(item['tag'],rules)
            if cat:
                item=copy.deepcopy(item)
                item['category']=cat['name']
                item.setdefault('variant',False)
                item['sources']=[item['source']]
                merged[item['tag']]=item
    if not isinstance(related,dict) or not isinstance(related.get('related_tags'),list):
        raise ValueError('invalid related_tag response')
    for row in related['related_tags']:
        tag_data=row.get('tag',{})
        tag=tag_data.get('name')
        if not tag or tag_data.get('category')!=0 or tag_data.get('is_deprecated'):
            continue
        cat=category_for(tag,rules)
        freq=row.get('frequency')
        if not cat or not isinstance(freq,(int,float)) or not 0<=freq<=1:
            continue
        evidence={'url':url,'method':'Danbooru related_tag general co-occurrence',
                  'frequency':freq,'query':related.get('query'),
                  'population_post_count':related.get('post_count'),
                  'limitations':rules['frequency_semantics']}
        if tag not in merged:
            merged[tag]={'tag':tag,'category':cat['name'],'variant':False,'sources':[]}
        item=merged[tag]
        item['sources'].append(evidence)
        item['frequency']=freq
        item['source']=evidence
    conflicts=[]
    blocked=set()
    groups={}
    for tag,item in merged.items():
        cat=category_for(tag,rules)
        if cat.get('outfit'):
            continue
        group=_group(tag,cat)
        if group:
            groups.setdefault(group,[]).append(item)
    for group,items in groups.items():
        # Low-frequency alternatives still count against dominance. A wiki-only
        # contradictory descriptor cannot be dismissed using an absent frequency.
        competitors=[x for x in items if x.get('frequency',0)>=rules['candidate_min_frequency'] or len(x['sources'])>1 or x.get('frequency') is None]
        if len(competitors)<2:
            continue
        tags = {x['tag'] for x in competitors}
        if any(tags <= set(compatible) for compatible in rules.get('compatible_tag_groups', [])):
            continue
        ordered=sorted(competitors,key=lambda x:x.get('frequency',-1),reverse=True)
        best,second=ordered[:2]
        a,b=best.get('frequency'),second.get('frequency')
        dominant=(a is not None and b is not None and
                  a-b>=rules['exclusive_min_gap'] and a>=b*rules['exclusive_min_ratio'])
        losers=ordered[1:] if dominant else ordered
        blocked.update(x['tag'] for x in losers)
        conflicts.append({'category':group,'tags':[x['tag'] for x in ordered],
                          'frequencies':{x['tag']:x.get('frequency') for x in ordered},
                          'decision':'dominant frequency' if dominant else 'unresolved; no lock',
                          'selected':best['tag'] if dominant else None})
    appearance=[];outfit=[];app_candidates=[];out_candidates=[]
    for tag,item in merged.items():
        cat=category_for(tag,rules)
        if cat.get('outfit'):
            item['locked'] = False
            item['decision'] = 'clothing candidate; default selection is separate'
            if (any(source['method'].startswith('linked tag') for source in item['sources'])
                    or item.get('frequency', 0) >= rules['candidate_min_frequency']):
                out_candidates.append(item)
            continue
        threshold=rules['min_frequency']
        freq=item.get('frequency')
        wiki_supported=any(s['method'].startswith('linked tag') for s in item['sources'])
        # Appearance alone retains the strict identity locking policy.
        # Clothing ensembles are selected separately below.
        lock=(tag not in blocked and ((freq is not None and freq>threshold)
              or (wiki_supported and not cat.get('outfit') and not item.get('variant'))))
        if cat.get('outfit') and item.get('variant'):
            lock=False
        item['locked']=lock
        item['decision']='locked' if lock else ('conflict' if tag in blocked else 'variant or below threshold')
        if lock:
            (outfit if cat.get('outfit') else appearance).append(item)
        elif wiki_supported or (freq is not None and freq>=rules['candidate_min_frequency']):
            (out_candidates if cat.get('outfit') else app_candidates).append(item)
    result.update(appearance_tags=appearance,default_outfit_tags=outfit,
                  appearance_candidates=app_candidates,outfit_candidates=out_candidates,
                  identity_conflicts=conflicts,identity_rules=copy.deepcopy(rules))
    from storycg.outfits import select_default_outfit
    result.update(select_default_outfit(identity.get('wiki_body',''), identity.get('wiki_url',''),
                                       related, url, rules))
    selected = {x['tag'] for x in result['default_outfit_tags']}
    result['outfit_candidates'] = [x for x in out_candidates if x['tag'] not in selected]
    return result
