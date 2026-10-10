"""Choose a sourced default clothing ensemble separately from identity appearance."""
import copy
import re


def _base_family(tag, config):
    for family in config['base_families']:
        if tag == family or tag.endswith('_' + family):
            return family
    return None


def wiki_ensembles(body, url, rules):
    from storycg.identity_enrichment import category_for
    config = rules['outfit_selection']
    ensembles = []
    for paragraph in re.split(r'\r?\n\s*\r?\n', body):
        current = None
        for sentence in re.split(r'(?<=[.!?])\s+', paragraph):
            tags = []
            sentence_tags=[re.sub(r'\s+','_',m.group(1).strip().lower()) for m in re.finditer(r'\[\[([^]|]+)(?:\|[^]]*)?\]\]',sentence)]
            if not re.search(config['description_pattern'],sentence,re.I) and not (current and any(category_for(t,rules) and category_for(t,rules)['name'] in ('accessories','accessory_color') for t in sentence_tags)):
                continue
            clothing_context=any(category_for(t,rules) and category_for(t,rules).get('outfit') for t in sentence_tags) or bool(re.search(config['new_ensemble_pattern'],sentence,re.I)) or current is not None
            for match in re.finditer(r'\[\[([^]|]+)(?:\|[^]]*)?\]\]', sentence):
                tag = re.sub(r'\s+', '_', match.group(1).strip().lower())
                category = category_for(tag, rules)
                if category and (category.get('outfit') or (clothing_context and category['name'] in ('accessories','accessory_color'))):
                    tags.append({'tag':tag, 'category':category['name'],
                                 'source':{'url':url, 'method':'linked tag in wiki clothing ensemble',
                                           'frequency':None, 'evidence':sentence}})
            if not tags:
                continue
            new = bool(re.search(config['new_ensemble_pattern'], sentence, re.I))
            if current is None and ensembles and re.search(config['continuation_pattern'], sentence, re.I) and not new:
                current = ensembles[-1]
            if current is None or new:
                current = {'id':f'outfit_{len(ensembles)+1:02d}', 'tags':[], 'sentences':[],
                           'historical':bool(re.search(config['historical_pattern'], sentence, re.I))}
                ensembles.append(current)
            current['sentences'].append(sentence)
            for item in tags:
                known = next((x for x in current['tags'] if x['tag']==item['tag']), None)
                if known:
                    known['sources'].append(item['source'])
                else:
                    item['sources'] = [item['source']]
                    current['tags'].append(item)
    return ensembles


def _score_ensemble(ensemble, frequencies, config):
    bases = [x['tag'] for x in ensemble['tags'] if _base_family(x['tag'], config)]
    # Generic parents can be shared by unrelated outfits. Prefer the specific
    # wiki descriptor, even if its frequency was not returned by the API.
    specific = [tag for tag in bases if tag != _base_family(tag, config)]
    candidates = specific or bases
    main = [tag for tag in candidates if _base_family(tag, config) not in config['layer_families']]
    candidates = main or candidates
    measured = [(tag, frequencies[tag]) for tag in candidates if tag in frequencies]
    selected = max(measured, key=lambda x:x[1]) if measured else None
    return {'candidates':[{'tag':tag, 'frequency':frequencies.get(tag)} for tag in candidates],
            'selected_base':{'tag':selected[0], 'frequency':selected[1]} if selected else None,
            'score':selected[1] if selected else None}


def _compatible(tag, selected, existing, rules):
    from storycg.identity_enrichment import _group, category_for
    config = rules['outfit_selection']
    family = _base_family(tag, config)
    primary = {_base_family(x, config) for x in existing
               if _base_family(x, config) and _base_family(x, config) not in config['layer_families']}
    if family and family not in config['layer_families'] and primary and family not in primary:
        return False
    for group in rules['outfit_incompatible_groups']:
        if tag in group and any(x in group and x != tag for x in existing):
            return False
    category = category_for(tag, rules)
    group = None if category['name']=='outfit_base' else _group(tag, category)
    if group and any(_group(x, category_for(x, rules)) == group and x != tag for x in existing):
        # A generic parent and its child, e.g. shirt / white_shirt, coexist.
        return False
    return True


def apply_same_garment_exclusion(result, rules):
    """V2-31: one colour per garment noun in the selected default outfit.

    Members sharing a garment noun (brown_shoes / black_shoes) are one garment. The kept member
    is the highest measured frequency, else the first in wiki order. The rest are demoted to
    unlocked outfit candidates: they stay out of prompts and review checklists.
    """
    from storycg.tags import color_split
    tags = result.get('default_outfit_tags', [])
    groups = {}
    for item in tags:
        split = color_split(item['tag'])
        if split:
            groups.setdefault(split[1], []).append(item)
    demoted, notes = [], []
    for noun, members in groups.items():
        if len(members) < 2:
            continue
        measured = [m for m in members if m.get('frequency') is not None]
        keep = max(measured, key=lambda m: m['frequency']) if measured else members[0]
        losers = [m for m in members if m is not keep]
        for m in losers:
            m.update(locked=False, decision=f"与 {keep['tag']} 互斥，降为备选",
                     exclusive_with=keep['tag'])
        demoted.extend(losers)
        notes.append({'garment': noun, 'kept': keep['tag'], 'kept_by': 'measured frequency' if measured else 'wiki order',
                      'demoted': [m['tag'] for m in losers]})
    if not demoted:
        return result
    kept_ids = {id(x) for x in tags} - {id(d) for d in demoted}
    result['default_outfit_tags'] = [x for x in tags if id(x) in kept_ids]
    result['outfit_candidates'] = demoted + list(result.get('outfit_candidates', []))
    result.setdefault('default_outfit_selection', {})['same_garment_exclusion'] = {
        'rule': rules['same_garment_color']['rule'], 'groups': notes}
    return result

def select_default_outfit(body, wiki_url, related, related_url, rules):
    from storycg.identity_enrichment import category_for
    config = rules['outfit_selection']
    frequency_rows = {row['tag']['name']:row for row in related['related_tags']
                      if row.get('tag',{}).get('category')==0 and
                      not row['tag'].get('is_deprecated') and
                      isinstance(row.get('frequency'), (int,float)) and 0<=row['frequency']<=1}
    frequencies = {tag:row['frequency'] for tag,row in frequency_rows.items()}
    ensembles = wiki_ensembles(body, wiki_url, rules)
    selection = {'kind':'default clothing choice; separate from appearance identity',
                 'statistics_url':related_url, 'tie_break':config['tie_break']}
    if ensembles:
        for ensemble in ensembles:
            ensemble['base_evidence'] = _score_ensemble(ensemble, frequencies, config)
        eligible = [x for x in ensembles if not x['historical']]
        if not eligible:
            return {'default_outfit_tags':[], 'alternate_outfits':ensembles,
                    'default_outfit_selection':{**selection,'method':'historical wiki outfits only',
                                                'reason':'no present-day default described'}}
        chosen = max(eligible, key=lambda x:x['base_evidence']['score'] if x['base_evidence']['score'] is not None else -1)
        default = copy.deepcopy(chosen['tags'])
        for item in default:
            item.update(locked=True, decision='selected default wiki ensemble')
            row = frequency_rows.get(item['tag'])
            if row:
                item['frequency'] = row['frequency']
                item['sources'].append({'url':related_url,'method':'Danbooru related_tag general co-occurrence',
                                        'frequency':row['frequency'],'query':related.get('query')})
        selection.update(method='single wiki ensemble' if len(eligible)==1 else 'wiki ensemble base frequency',
                         selected_ensemble=chosen['id'], selected_base=chosen['base_evidence']['selected_base'],
                         ensembles=[{'id':e['id'],'historical':e['historical'],**e['base_evidence']} for e in ensembles],
                         reason='whole wiki ensemble selected; measured specific main garment frequency, then wiki order; historical outfits excluded')
        alternate = [copy.deepcopy(x) for x in ensembles if x is not chosen]
        for ensemble in alternate:
            for item in ensemble['tags']:
                item.update(locked=False, decision='alternate wiki ensemble')
        return {'default_outfit_tags':default, 'alternate_outfits':alternate,
                'default_outfit_selection':selection}
    bases = [tag for tag in frequencies if category_for(tag,rules) and
             category_for(tag,rules)['name']=='outfit_base']
    if not bases:
        return {'default_outfit_tags':[], 'alternate_outfits':[],
                'default_outfit_selection':{**selection,'method':'solo co-occurrence fallback',
                                            'reason':'no wiki clothing and no measured outfit_base'}}
    base = max(bases,key=lambda tag:frequencies[tag])
    selected = [base]
    for tag in sorted(frequencies,key=lambda tag:-frequencies[tag]):
        category = category_for(tag,rules)
        if (tag==base or not category or not category.get('outfit') or
            frequencies[tag]<config['component_min_frequency']):
            continue
        if _compatible(tag, base, selected, rules):
            selected.append(tag)
    tags = []
    for tag in selected:
        source = {'url':related_url,'method':'Danbooru related_tag general co-occurrence',
                  'frequency':frequencies[tag],'query':related.get('query'),
                  'limitations':rules['frequency_semantics']}
        tags.append({'tag':tag,'category':category_for(tag,rules)['name'],
                     'frequency':frequencies[tag],'source':source,'sources':[source],
                     'locked':True,'decision':'selected default co-occurrence ensemble'})
    selection.update(method='solo co-occurrence fallback', selected_base={'tag':base,'frequency':frequencies[base]},
                     component_min_frequency=config['component_min_frequency'],
                     reason='no wiki clothing; highest frequency outfit_base with compatible components')
    return {'default_outfit_tags':tags,'alternate_outfits':[],'default_outfit_selection':selection}
