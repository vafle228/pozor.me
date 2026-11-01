"""Opt-in model comparisons; shipped synthetic cases never stand in for real votes."""

import asyncio
import json
import random
import time
from pathlib import Path
from typing import Any

from pozor.config import Settings
from pozor.core.domain.context import ContextPack, Evidence
from pozor.core.usecases.generate import Generator
from pozor.infra.files.prompts import FilePromptStore
from pozor.infra.llm.client import LlamaClient


def load_cases(dataset: Path) -> list[dict[str, Any]]:
    cases = [json.loads(line) for line in dataset.read_text('utf-8').splitlines() if line.strip()]
    if len({case['id'] for case in cases}) != len(cases):
        raise ValueError('duplicate case id')
    if any(not isinstance(case.get('trigger'),str) or not case['trigger'].strip() for case in cases):
        raise ValueError('each case requires a trigger')
    return cases


async def compare(dataset: Path, variants: list[str], destination: Path, settings: Settings) -> dict[str, Any]:
    cases = load_cases(dataset)
    if len(variants)!=2 or any(v not in {'A','B'} for v in variants):
        raise ValueError('variants: A (legacy v1) and B (candidates v2)')
    output = []
    for case in cases:
        pack = ContextPack(chat_id=-1,thread_id=0,target_id=42,target_label='Участник',
            trigger=Evidence(evidence_id=f"trigger:{case['id']}",chat_id=-1,allowed_target_id=42,
                source_namespace='request',relation='trigger',attribution='caller_hint',text=case['trigger']))
        answers = []
        for variant in variants:
            snapshot = settings.model_copy(update={'humor_v2_enabled':variant=='B'})
            async with LlamaClient(snapshot) as client:
                result = await Generator(client,random.SystemRandom(),FilePromptStore()).generate(
                    pack,asyncio.get_running_loop().time()+snapshot.generation_deadline_seconds)
                answers.append({'variant':variant,'text':result.roast.text,'angle':result.roast.angle,
                    'fallback':result.fallback,'latency_ms':result.latency_ms})
        random.SystemRandom().shuffle(answers)
        output.append({'case_id':case['id'],'trigger':case['trigger'],'left':answers[0],'right':answers[1],
                       'vote':None})
    report = {'created_at':time.time(),'dataset':dataset.name,'pairs':output,
              'acceptance':{'minimum_pairs':40,'minimum_win_rate':.6,'requires_human_votes':True}}
    destination.parent.mkdir(parents=True,exist_ok=True)
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n','utf-8')
    return {'pairs':len(output),'output':str(destination),'status':'awaiting_blind_human_votes'}


async def publish(path: Path, chat: int, settings: Settings) -> None:
    from aiogram import Bot
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from pozor.infra.sqlite import humor_eval_repo as repository
    from pozor.infra.sqlite.connection import migrate
    if settings.bot_token is None:
        raise ValueError('BOT_TOKEN is required for explicit review publication')
    await asyncio.to_thread(migrate,settings.database_path)
    report=json.loads(path.read_text('utf-8'))
    async with Bot(token=settings.bot_token.get_secret_value()) as bot:
        for pair in report['pairs']:
            pair_id=await asyncio.to_thread(repository.pair,settings.database_path,chat,
                pair['left']['variant'],pair['right']['variant'],str(pair['case_id']))
            message=await bot.send_message(chat_id=chat,parse_mode=None,
                text=f"Слепое сравнение\nРеплика: {pair['trigger']}\n\nA: {pair['left']['text']}\n\nB: {pair['right']['text']}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text=label,callback_data=f'eval:{pair_id}:{value}')
                    for label,value in [('A','A'),('B','B'),('Равны','tie')]]]))
            await asyncio.to_thread(repository.sent,settings.database_path,pair_id,message.message_id)
            await asyncio.sleep(3)
