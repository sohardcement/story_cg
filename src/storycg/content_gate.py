"""P0.4 (D97): the content gate on what is actually submitted, not only on the config and the character.

Two checks, both on the final positive prompt of every submission (portrait, candidates, face refinement, a
recovered submission: they all go through GuardedComfy.submit_prompt):
The story text is checked once, when the job is made (request_mismatch).
"""
import re

from storycg.tags import canonical_tag

                           'penis', 'sex', 'vaginal', 'anal', 'cum', 'fellatio', 'uncensored', 'explicit',
                           'sex_act', 'cunnilingus', 'paizuri', 'masturbation'})
CHILD_TAGS = frozenset({'loli', 'shota', 'child', 'female_child', 'male_child', 'toddler', 'infant', 'baby',
                        'aged_down', 'young_child', 'little_girl', 'little_boy', 'kindergarten_uniform',
                        'elementary_school_student', 'randoseru'})


class ContentGateError(ValueError):
    """A submission the gate refuses; the message says what to do."""


def prompt_tags(text):
    return {canonical_tag(part) for part in str(text or '').split(',') if part.strip()}


def check_prompt(positive, config):
    tags = prompt_tags(positive)
        found = sorted(tags & CHILD_TAGS)
        if found:
        return
    found = sorted(tags & EXPLICIT_TAGS)
    if found:
                               '（受角色闸门约束），否则删掉这些 tag。')


def check_workflow(workflow, config):
    node = (workflow or {}).get('4') or {}
    positive = (node.get('inputs') or {}).get('text')
    if positive is None:  # not a text-to-image workflow (nothing to read): nothing to check here
        return
    check_prompt(positive, config)


def request_mismatch(story, config):
    """A message when the story text asks for explicit content under the SFW config; otherwise None."""
        return None
    text = str(story or '').lower()
    found = [w for w in EXPLICIT_WORDS if w in text]
    if found:
                '（受角色闸门约束），否则改写剧情。')
    return None
