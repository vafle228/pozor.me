"""Scenario contracts for the candidate editor, independent of SQLite and HTTP."""

import asyncio
import json

from pozor.config import Settings
from pozor.core.domain.context import ContextPack, Evidence
from pozor.core.humor.candidates import MECHANISMS
from pozor.core.usecases.generate import Generator


class FakeModel:
    def __init__(self, answers):
        self.settings = Settings(_env_file=None)
        self.answers = iter(answers)
        self.calls = []

    async def tokens(self, text, deadline):
        return len(text.split())

    async def count(self, messages, deadline):
        return sum(len(m['content'].split()) for m in messages)

    async def complete_json(self, messages, deadline, **kwargs):
        self.calls.append((messages, kwargs))
        answer=next(self.answers)
        if answer.get('candidates'):
            answer=dict(answer)
            answer['candidates']=[*answer['candidates']]
            while len(answer['candidates'])<5:
                answer['candidates'].append(answer['candidates'][0])
        return json.dumps(answer, ensure_ascii=False)

    async def complete(self, messages, deadline):
        raise AssertionError('V2 uses distinct schemas for drafts and judge')


def context(**changes):
    return ContextPack(chat_id=-1, thread_id=0, target_id=42, target_label='Алиса',
        trigger=Evidence(evidence_id='trigger:1', chat_id=-1, allowed_target_id=42,
            source_namespace='request', relation='trigger', attribution='caller_hint',
            text='Опять проспала созвон'), **changes)


def candidate(text, index=0, angle='совещание будильников'):
    return {'mechanism': MECHANISMS[index], 'angle': angle, 'text': text}


async def test_editor_selects_concrete_punch_and_code_addresses_target():
    model = FakeModel([{'candidates': [
        candidate('Семь будильников провели совещание и решили дать тебе ещё пять минут.'),
        candidate('Твой будильник уже подал заявление на отпуск.', 1, 'отпуск будильника')]},
        {'best': 1, 'reject': [0], 'risk': [], 'reason': 'коротко и конкретно'}])
    result = await Generator(model).generate(context(), asyncio.get_running_loop().time()+2)
    assert not result.fallback
    assert result.roast.text == 'Алиса, Твой будильник уже подал заявление на отпуск.'
    assert len(result.candidates) == 5 and result.prompt_version == 'v2'
    assert model.calls[1][1]['temperature'] == 0


async def test_hard_rejected_candidate_never_reaches_judge_or_chat():
    model = FakeModel([{'candidates': [candidate('Тебе поставили диагноз шизофрения.'),
        candidate('Будильник перевёл тебя в архив пропущенных звонков.', 1)]},
        {'best': 0, 'reject': [], 'risk': [], 'reason': 'конкретный образ'}])
    result = await Generator(model).generate(context(), asyncio.get_running_loop().time()+2)
    assert not result.fallback and 'диагноз' not in result.roast.text
    assert 'диагноз' not in model.calls[1][0][1]['content']


async def test_judge_risk_cannot_be_selected_even_after_second_round():
    batch = {'candidates': [candidate('Твой будильник уже подал заявление на отпуск.')]}
    model = FakeModel([batch, {'best': 0, 'reject': [], 'risk': [0], 'reason': 'опасно'},
        {'candidates': []}])
    result = await Generator(model).generate(context(), asyncio.get_running_loop().time()+2)
    assert result.fallback and 'Не удалось' not in result.roast.text


async def test_dialogue_has_its_own_simple_schema():
    model = FakeModel([{'text': 'Привет! У будильников сегодня выходной?'}])
    result = await Generator(model).generate(context(mode='dialogue'), asyncio.get_running_loop().time()+2)
    assert not result.fallback
    assert list(model.calls[0][1]['schema']['properties']) == ['text']
