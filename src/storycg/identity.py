"""Resolve a character tag and source identity descriptors from Danbooru."""

import copy
import hashlib
import json
import re
import time
from pathlib import Path

import httpx

from storycg.net_errors import json_body
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


def needs_user(root, identity, reason, *, question=None, persist=True):
    """Stop the job and ask the user (V2-38: the L3/L4 route is gone; this is the only stop left).

    The stop keeps the identity's evidence and writes identity.json unless persist is False.
    """
    identity = copy.deepcopy(identity)
    identity.update(status='needs_user', needs_user=True, reason=reason,
                    question=question or (reason + '。'), user_can_provide=[])
    if persist:
        save_json_atomic(Path(root)/'identity.json', identity)
    return identity


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
        payload = json_body(response)
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


def _danbooru_for_list_tag(source, list_tag):
    """V2-25: the one real Danbooru character tag whose normalized form equals the WAI list tag, else None."""
    from storycg import wai_list
    entries,_=source.get('/autocomplete.json',{'search[query]':list_tag,'search[type]':'tag_query','limit':10})
    want=wai_list.normalize(list_tag)
    matches={x['value'] for x in entries if x.get('category')==4 and not x.get('tag',{}).get('is_deprecated')
             and wai_list.normalize(x.get('value',''))==want}
    return matches.pop() if len(matches)==1 else None


IDENTITY_TAG_BATCH = 100


def drop_unavailable_identity_tags(result, source):
    """V2-51 (D88): remove appearance and standard outfit tags that Danbooru does not offer.

    Same rule as TagRegistry.lookup: a tag is available only when an entry with the same name is not deprecated.
    Removed tags go to result['unavailable_identity_tags'] as {tag, field, reason ('deprecated' or 'not_found'), source}.
    Names with a comma cannot go in name_comma, so they are asked one by one with search[name].
    """
    fields = ('appearance_tags', 'default_outfit_tags')
    names = sorted({item['tag'] for field in fields for item in result[field]})
    verdict = {}
    plain = [name for name in names if ',' not in name]
    queries = []
    for start in range(0, len(plain), IDENTITY_TAG_BATCH):
        batch = plain[start:start + IDENTITY_TAG_BATCH]
        queries.append((batch, {'search[name_comma]': ','.join(batch), 'limit': len(batch)}))
    queries += [([name], {'search[name]': name, 'limit': 1}) for name in names if ',' in name]
    for batch, params in queries:
        payload, url = source.get('/tags.json', params)
        for name in batch:
            matches = [item for item in payload if item.get('name') == name]
            if any(not item.get('is_deprecated', False) for item in matches):
                verdict[name] = ('available', url)
            else:
                verdict[name] = ('deprecated' if matches else 'not_found', url)
    unavailable = []
    for field in fields:
        kept = []
        for item in result[field]:
            status, url = verdict[item['tag']]
            if status == 'available':
                kept.append(item)
            else:
                unavailable.append({'tag': item['tag'], 'field': field, 'reason': status, 'source': url})
        result[field] = kept
    result['unavailable_identity_tags'] = unavailable
    return result


def resolve_identity(name: str, cache_dir: str | Path, *, client: httpx.Client | None = None,
                     work: str | None = None, character_tag: str | None = None,
                     rules: dict | None = None, wai: dict | None = None) -> dict:
    """Resolve candidates without letting an exact other-name hide ambiguity.

    No generation or vision service is contacted here. Tier verification remains pending.
    `wai` is the job's `wai_list` config block (None: the default config). V2-25: a name that hits exactly one
    WAI list row resolves through that row's tag to a real Danbooru tag; anything else follows the old path.
    """
    from storycg.identity_enrichment import load_rules, wiki_descriptors, enrich_identity
    from storycg import wai_list
    if not name.strip():
        raise ValueError('character name is empty')
    rules=rules or load_rules()
    if client is None:
        with httpx.Client(base_url=BASE_URL,timeout=30,trust_env=False,
                          headers={'User-Agent':USER_AGENT}) as owned:
            return resolve_identity(name,cache_dir,client=owned,work=work,
                                    character_tag=character_tag,rules=rules,wai=wai)
    source=_CachedDanbooru(Path(cache_dir),client)
    if character_tag is None and work is None:
        hit=wai_list.lookup_name(name,wai_list.settings_for(wai))
        if hit:
            danbooru=_danbooru_for_list_tag(source,hit['list_tag'])
            if danbooru:
                result=resolve_identity(name,cache_dir,client=client,character_tag=danbooru,rules=rules,wai=wai)
                result['wai_list']={**hit,'danbooru_tag':danbooru}
                return result
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
    if not choices and not candidates:
        return needs_user(Path(cache_dir), {'input_name':name,'candidates':candidates,
            'appearance_tags':[],'default_outfit_tags':[]}, '找不到该角色的资料',
            question='找不到该角色的资料。请换一个角色名、角色 tag 或作品名。', persist=False)
    if len(choices)!=1:
        return {'input_name':name,'status':'needs_clarification','work_input':work,
                'reason':'no matching character candidate' if not choices else 'ambiguous character candidates',
                'candidates':candidates,'candidate_limit':10,'candidate_list_may_be_truncated':len(entries)>=10,
                'appearance_tags':[],'default_outfit_tags':[]}
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
            'candidates':candidates}
    related,related_url=source.get('/related_tag.json',{'query':' '.join(x for x in [character,rules.get('related_query_suffix','')] if x),'category':0})
    result=enrich_identity(result,related,related_url,rules)
    drop_unavailable_identity_tags(result,source)
    result['identity_block']=', '.join(x for x in ['1girl',character,chosen.get('work_tag'),
                                     *(item['tag'] for item in result['appearance_tags'])] if x)
    result['outfit_block']=', '.join(item['tag'] for item in result['default_outfit_tags'])
    return result
