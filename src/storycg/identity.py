"""Resolve a character tag and source identity descriptors from Danbooru."""

import hashlib
import json
import re
import time
from pathlib import Path

import httpx

from storycg.state import save_json_atomic


BASE_URL = "https://danbooru.donmai.us"
USER_AGENT = "storycg/0.1 (local story-CG identity resolver)"
WIKI_LINK = re.compile(r"\[\[([^]|]+)(?:\|[^]]*)?\]\]")
APPEARANCE = re.compile(
    r"(?:^|_)(?:hair|eyes?|ears?|skin|horns?|tails?|elf|petite|bangs|"
    r"twintails?|ponytails?|braids?|ahoge|fangs?|freckles|eyebrows?|"
    r"eyelashes|mole|glasses)(?:_|$)"
)
APPAREL = re.compile(
    r"(?:^|_)(?:shirt|jacket|skirt|belt|capelet|cape|collar|pantyhose|"
    r"boots?|shoes?|earrings?|dress|coat|cloak|robe|uniform|socks?|"
    r"gloves?|hat|scarf|ribbon|gem|trim)(?:_|$)"
)


def _tag(text: str) -> str:
    return re.sub(r"\s+", "_", text.strip().lower())


class _CachedDanbooru:
    def __init__(self, cache_dir: Path, client: httpx.Client):
        self.cache_dir = cache_dir
        self.client = client
        self.last_request = 0.0

    def get(self, path: str, params: dict | None = None) -> tuple[object, str]:
        url = str(self.client.build_request("GET", path, params=params).url)
        cache = self.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))["response"], url
        elapsed = time.monotonic() - self.last_request
        if self.last_request and elapsed < 0.8:
            time.sleep(0.8 - elapsed)
        response = self.client.get(path, params=params, headers={"User-Agent": USER_AGENT})
        self.last_request = time.monotonic()
        response.raise_for_status()
        payload = response.json()
        save_json_atomic(cache, {"url": url, "response": payload})
        return payload, url


def _appearance_from_wiki(body: str, wiki_url: str) -> list[dict]:
    found = []
    seen = set()
    for paragraph in re.split(r"\r?\n\s*\r?\n", body):
        if not re.search(r"\b(?:has|is a|is an|with|features)\b", paragraph, re.I):
            continue
        for match in WIKI_LINK.finditer(paragraph):
            tag = _tag(match.group(1))
            if tag not in seen and APPEARANCE.search(tag):
                found.append({"tag": tag, "source": {"url": wiki_url,
                              "method": "linked tag in character wiki appearance prose",
                              "evidence": match.group(0)}})
                seen.add(tag)
    return found


def _outfit_from_wiki(body: str, wiki_url: str) -> list[dict]:
    found = []
    seen = set()
    for paragraph in re.split(r"\r?\n\s*\r?\n", body):
        if not re.search(r"\b(?:wears?|outfit|clothing|dressed)\b", paragraph, re.I):
            continue
        for match in WIKI_LINK.finditer(paragraph):
            tag = _tag(match.group(1))
            if tag not in seen and APPAREL.search(tag):
                found.append({"tag": tag, "source": {"url": wiki_url,
                              "method": "linked tag in character wiki clothing prose",
                              "evidence": match.group(0)}})
                seen.add(tag)
    return found


def _wiki(source, character):
    from urllib.parse import quote
    path=f"/wiki_pages/{quote(character, safe='')}.json"
    try:
        wiki,url=source.get(path)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        wiki,url={},str(source.client.build_request('GET',path).url)
    return wiki,url


def _work(body, character, wiki_url):
    # Only the introductory paragraphs identify a work. Descriptive phrases
    # such as "made up of [[hair tag]]" must never become a work attribution.
    intro = re.split(r"\b(?:She|He|They|\w+) has\b|h4\.", body, maxsplit=1)[0]
    patterns = [
        r"\[\[([^]|]+)(?:\|[^]]*)?\]\]\s+character\b",
        r"\b(?:character|characters|agent|protagonist)\b[^.\n]{0,120}?\b(?:of|from|in)\s+(?:the\s+)?\[\[([^]|]+)",
        r"\b(?:of|from|in)\s+(?:the\s+)?\[\[([^]|]+)",
    ]
    match = next((m for pattern in patterns if (m := re.search(pattern, intro, re.I))), None)
    if match:
        return _tag(match.group(1)), {'url':wiki_url,'method':'work link in character wiki','evidence':match.group(0)}
    parenthetical=re.search(r"\(([^)]+)\)$",character)
    return ((_tag(parenthetical.group(1)), {'url':wiki_url,'method':'character tag parenthetical'})
            if parenthetical else (None,None))


def resolve_identity(name: str, cache_dir: str | Path, *, client: httpx.Client | None = None,
                     work: str | None = None, character_tag: str | None = None,
                     rules: dict | None = None) -> dict:
    """Resolve candidates without letting an exact other-name hide ambiguity.

    No generation or vision service is contacted here. Tier verification remains pending.
    """
    from storycg.identity_enrichment import load_rules, wiki_descriptors, enrich_identity
    if not name.strip():
        raise ValueError('character name is empty')
    rules=rules or load_rules()
    if client is None:
        with httpx.Client(base_url=BASE_URL,timeout=30,trust_env=False,
                          headers={'User-Agent':USER_AGENT}) as owned:
            return resolve_identity(name,cache_dir,client=owned,work=work,
                                    character_tag=character_tag,rules=rules)
    source=_CachedDanbooru(Path(cache_dir),client)
    search=character_tag or name
    entries,search_url=source.get('/autocomplete.json',{
        'search[query]':search,'search[type]':'tag_query','limit':10})
    candidates=[]
    for item in entries:
        if item.get('category')!=4 or item.get('tag',{}).get('is_deprecated'):
            continue
        tag=item['value']
        if any(x['tag']==tag for x in candidates):
            continue
        wiki,wiki_url=_wiki(source,tag)
        work_tag,work_source=_work(wiki.get('body',''),tag,wiki_url)
        candidates.append({'tag':tag,'post_count':item.get('post_count',0),
                           'antecedent':item.get('antecedent'),'other_names':wiki.get('other_names',[]),
                           'work_tag':work_tag,'work_source':work_source,
                           'reason':f"character category; alias {item.get('antecedent') or tag}; work {work_tag or 'unknown'}",
                           'source':{'url':search_url,'method':'autocomplete character category and other names'}})
    # Validate direct tags by the tag API. English display names that normalize
    # to an actual tag also work, while alias exactness never overrides candidates.
    direct=_tag(search)
    direct_candidates=[c for c in candidates if c['tag']==direct]
    if character_tag or direct_candidates:
        tags,tags_url=source.get('/tags.json',{'search[name]':direct,'limit':1})
        valid=next((t for t in tags if t.get('name')==direct and t.get('category')==4 and not t.get('is_deprecated')),None)
        if valid:
            if not direct_candidates:
                wiki,wiki_url=_wiki(source,direct)
                wt,ws=_work(wiki.get('body',''),direct,wiki_url)
                direct_candidates=[{'tag':direct,'post_count':valid.get('post_count',0),'work_tag':wt,'work_source':ws,
                                    'other_names':wiki.get('other_names',[]),'reason':'explicit character tag validated by tags API',
                                    'source':{'url':tags_url,'method':'exact character category tag'}}]
            choices=direct_candidates
        else:
            choices=[] if character_tag else candidates
    else:
        choices=candidates
    if work:
        work_entries,work_url=source.get('/autocomplete.json',{
            'search[query]':work,'search[type]':'tag_query','limit':10})
        work_tags={_tag(work)} | {x['value'] for x in work_entries if x.get('category')==3}
        choices=[c for c in choices if c.get('work_tag') in work_tags]
    pending={'identity_tier':'tier_pending_comfyui','tier_evidence':{
        'status':'not_run','reason':'ComfyUI unavailable; live LoRA metadata and unconditioned L2 generation required',
        'generation_count':0,'vision_calls':0}}
    if len(choices)!=1:
        return {'input_name':name,'status':'needs_clarification','work_input':work,
                'reason':'no matching character candidate' if not choices else 'ambiguous character candidates',
                'candidates':candidates,'candidate_limit':10,'candidate_list_may_be_truncated':len(entries)>=10,
                'appearance_tags':[],'default_outfit_tags':[],**pending}
    chosen=choices[0]
    character=chosen['tag']
    wiki,wiki_url=_wiki(source,character)
    descriptors=wiki_descriptors(wiki.get('body',''),wiki_url,rules)
    from storycg.identity_enrichment import category_for
    appearance=[x for x in descriptors if not category_for(x['tag'],rules).get('outfit')]
    outfit=[x for x in descriptors if category_for(x['tag'],rules).get('outfit')]
    result={'input_name':name,'status':'locked','character_tag':character,
            'character_source':chosen['source'],'work_tag':chosen.get('work_tag'),
            'work_source':chosen.get('work_source'),'appearance_tags':appearance,'default_outfit_tags':outfit,
            'wiki_body':wiki.get('body',''),'wiki_url':wiki_url,
            'wiki_baseline':{'appearance_tags':_appearance_from_wiki(wiki.get('body',''),wiki_url),
                             'default_outfit_tags':_outfit_from_wiki(wiki.get('body',''),wiki_url)},
            'lora':None,'reference_image':None,'candidates':candidates,**pending}
    related,related_url=source.get('/related_tag.json',{'query':' '.join(x for x in [character,rules.get('related_query_suffix','')] if x),'category':0})
    result=enrich_identity(result,related,related_url,rules)
    result['identity_block']=', '.join(x for x in ['1girl',character,chosen.get('work_tag'),
                                     *(item['tag'] for item in result['appearance_tags'])] if x)
    result['outfit_block']=', '.join(item['tag'] for item in result['default_outfit_tags'])
    return result
