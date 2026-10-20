"""P0.4 (D97): the content gate on what is actually submitted, not only on the config and the character.

One check on the final positive prompt of every submission (portrait, candidates, face refinement, a
recovered submission: they all go through GuardedComfy.submit_prompt): a prompt that carries an explicit tag
is refused. The negative prompt never counts: those tags there are a request to avoid them.
The story text is checked once, when the job is made (request_mismatch).
"""
from storycg.tags import canonical_tag

EXPLICIT_TAGS = frozenset({'nsfw', 'nude', 'naked', 'completely_nude', 'topless', 'bottomless', 'nipples', 'pussy',
                           'penis', 'sex', 'vaginal', 'anal', 'cum', 'fellatio', 'uncensored', 'explicit',
                           'sex_act', 'cunnilingus', 'paizuri', 'masturbation'})
EXPLICIT_WORDS = ('裸体', '全裸', '半裸', '裸露', '赤裸', '色情', '情色', '性爱', '做爱', '性交')


class ContentGateError(ValueError):
    """A submission the gate refuses; the message says what to do."""


def prompt_tags(text):
    return {canonical_tag(part) for part in str(text or '').split(',') if part.strip()}


def check_prompt(positive, config):
    found = sorted(prompt_tags(positive) & EXPLICIT_TAGS)
    if found:
        raise ContentGateError(f'提示词里有不允许的 tag（{", ".join(found)}），不提交。删掉这些 tag。')


def check_workflow(workflow, config):
    node = (workflow or {}).get('4') or {}
    positive = (node.get('inputs') or {}).get('text')
    if positive is None:  # not a text-to-image workflow (nothing to read): nothing to check here
        return
    check_prompt(positive, config)


def request_mismatch(story, config):
    """A message when the story text asks for content this project does not draw; otherwise None."""
    text = str(story or '').lower()
    found = [w for w in EXPLICIT_WORDS if w in text]
    if found:
        return f'剧情里有不处理的内容字眼（{"、".join(found)}），请改写剧情。'
    return None
