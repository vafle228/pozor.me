"""Private generation journal, seven-day repetition memory, and scoped ratings."""

import json
import time
from pathlib import Path
from typing import Any

from pozor import __version__
from pozor.config import Settings
from pozor.core.domain.context import ContextPack, ConversationTurn
from pozor.core.domain.generation import GenerationResult
from pozor.infra.sqlite.connection import connect
from pozor.infra.sqlite.retrieval import sources_current


class GenerationRepository:
    def __init__(self, database: Path):
        self.database = database

    def record(self, pack: ContextPack, result: GenerationResult, settings: Settings,
               session_id: str, turn_no: int, revision: int | None = None) -> int | None:
        with connect(self.database) as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute("SELECT s.state,s.revision,c.allowed,c.enabled,m.consent,m.opt_out,coalesce(e.epoch,0) AS epoch FROM sessions s "
                "JOIN members m ON m.chat_id=s.chat_id AND m.user_id=s.target_id "
                "JOIN chats c ON c.chat_id=s.chat_id "
                "LEFT JOIN data_epochs e ON e.chat_id=s.chat_id AND e.target_id=s.target_id WHERE s.id=?",
                (session_id,)).fetchone()
            ids = [e.evidence_id for e in [*pack.recent,*pack.evidence]]
            ids += [f'message:{h.source_id}' for h in pack.hooks]
            if (not current or current['state'] != 'active' or not current['consent'] or current['opt_out']
                    or not current['allowed'] or not current['enabled']
                    or revision is not None and current['revision']!=revision
                    or current['epoch'] != pack.evidence_epoch
                    or not sources_current(db,pack.chat_id,pack.target_id,ids,time.time())):
                return None
            for evidence in [*pack.recent,*pack.evidence]:
                if not evidence.text_hash:
                    continue
                if evidence.evidence_id.startswith('message:'):
                    source=db.execute('SELECT text_hash FROM messages WHERE id=?',(evidence.evidence_id[8:],)).fetchone()
                else:
                    source=db.execute('SELECT text_hash FROM manual_items WHERE item_id=?',(evidence.evidence_id[7:],)).fetchone()
                if not source or source['text_hash']!=evidence.text_hash:
                    return None
            for hook in pack.hooks:
                if not db.execute("SELECT 1 FROM profile_items p JOIN profile_item_sources s ON s.card_id=p.id "
                    "WHERE p.chat_id=? AND p.target_id=? AND p.kind='hook' AND p.excluded=0 AND p.quote=? AND s.message_id=?",
                    (pack.chat_id,pack.target_id,hook.quote,hook.source_id)).fetchone():
                    return None
            for memory in pack.dossier:
                if not db.execute("SELECT 1 FROM profile_items WHERE chat_id=? AND target_id=? AND kind='dossier' "
                    "AND excluded=0 AND summary=?",(pack.chat_id,pack.target_id,memory.summary)).fetchone():
                    return None
            version = db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0]
            values = (pack.chat_id, pack.target_id, session_id, turn_no, pack.mode,
                result.prompt_version, __version__, version, result.model_name or settings.llm_model,
                json.dumps(result.sampling_params or {k: getattr(settings, k) for k in ('temperature','top_p','min_p','presence_penalty','max_output_tokens')}),
                result.request_json or pack.model_dump_json(exclude={'repetition_texts'}), json.dumps(result.candidates, ensure_ascii=False),
                json.dumps(result.roast.model_dump()|{'mechanism':result.selected_mechanism},ensure_ascii=False), result.judge_reason, result.latency_ms,
                result.reason, time.time())
            db.execute('''INSERT INTO generations(chat_id,target_id,session_id,turn_no,mode,prompt_version,
                code_version,schema_version,model,params_json,request_json,candidates_json,selected_json,
                judge_reason,latency_ms,fallback_reason,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(session_id,turn_no) DO UPDATE SET request_json=excluded.request_json,
                params_json=excluded.params_json,candidates_json=excluded.candidates_json,
                selected_json=excluded.selected_json,judge_reason=excluded.judge_reason,
                latency_ms=excluded.latency_ms,fallback_reason=excluded.fallback_reason,
                created_at=excluded.created_at''', values)
            return int(db.execute('SELECT id FROM generations WHERE session_id=? AND turn_no=?',
                                  (session_id, turn_no)).fetchone()[0])

    def memory(self, chat: int, target: int) -> tuple[list[str], list[str], set[int], list[ConversationTurn]]:
        now = time.time()
        angles: list[str] = []
        answers: list[str] = []
        sources: set[int] = set()
        examples: list[ConversationTurn] = []
        with connect(self.database) as db:
            rows = db.execute('''SELECT g.*, (SELECT count(*) FROM joke_ratings r WHERE
                r.generation_id=g.id AND r.rating=1) AS positive FROM generations g
                JOIN turns t ON t.session_id=g.session_id AND t.turn_no=g.turn_no
                WHERE g.chat_id=? AND g.mode='roast' AND g.created_at>? AND t.status='sent'
                ORDER BY g.created_at DESC''', (chat, now-7*86400)).fetchall()
            own = 0
            for row in rows:
                request = json.loads(row['request_json'])
                selected = json.loads(row['selected_json'])
                ids = [e['evidence_id'] for name in ('recent','evidence') for e in request.get(name, [])]
                if not sources_current(db, chat, row['target_id'], ids, now):
                    continue
                if selected['angle'] != 'fallback' and len(angles) < 8:
                    angles.append(selected['angle'])
                answers.append(selected['text'])
                if row['target_id'] == target and own < 10:
                    own += 1
                    sources.update(h['source_id'] for h in request.get('hooks', []))
                    if row['positive'] >= 2 and len(examples) < 4:
                        # Never inject another person's personal material as this target's example.
                        examples += [ConversationTurn(role='user', text=request['trigger']['text']),
                                     ConversationTurn(role='assistant', text=json.dumps({'candidates': [{
                                         'mechanism': 'возврат к цитате', 'angle': selected['angle'],
                                         'text': selected['text']}]}, ensure_ascii=False))]
        return angles, answers, sources, examples

    def rate(self, generation_id: int, *, chat: int, thread: int, user: int,
             message_id: int, rating: int) -> bool:
        if rating not in (-1,0,1):
            return False
        with connect(self.database) as db:
            valid = db.execute('''SELECT 1 FROM generations g JOIN sessions s ON s.id=g.session_id
                JOIN outbox o ON o.session_id=g.session_id AND o.turn_no=g.turn_no
                JOIN chats c ON c.chat_id=s.chat_id JOIN members m ON m.chat_id=c.chat_id AND m.user_id=?
                WHERE g.id=? AND s.chat_id=? AND s.thread_id=? AND o.status='sent'
                AND o.telegram_message_id=? AND c.allowed=1 AND c.enabled=1 AND m.consent=1 AND m.opt_out=0
                AND NOT EXISTS (SELECT 1 FROM forgotten_members f WHERE f.chat_id=c.chat_id AND f.user_id=m.user_id)''',
                (user,generation_id,chat,thread,message_id)).fetchone()
            if not valid:
                return False
            db.execute('INSERT INTO joke_ratings VALUES (?,?,?,?) ON CONFLICT(generation_id,user_id) '
                       'DO UPDATE SET rating=excluded.rating,updated_at=excluded.updated_at',
                       (generation_id,user,rating,time.time()))
            return True

    def mechanisms(self,chat: int) -> list[str]:
        with connect(self.database) as db:
            rows=db.execute("SELECT g.selected_json FROM generations g JOIN turns t ON t.session_id=g.session_id "
                "AND t.turn_no=g.turn_no WHERE g.chat_id=? AND t.status='sent' AND g.mode='roast' "
                "AND g.created_at>? ORDER BY g.created_at DESC LIMIT 8",(chat,time.time()-7*86400)).fetchall()
        return list(dict.fromkeys(json.loads(r[0]).get('mechanism','') for r in rows if json.loads(r[0]).get('mechanism')))[:2]

    def metrics(self) -> dict[str, Any]:
        with connect(self.database) as db:
            rows = db.execute('SELECT latency_ms,fallback_reason,selected_json FROM generations').fetchall()
            ratings = db.execute('SELECT rating,count(*) AS count FROM joke_ratings GROUP BY rating').fetchall()
        times = sorted(r['latency_ms'] for r in rows)
        angles=[json.loads(r['selected_json'])['angle'] for r in rows if r['fallback_reason'] is None]
        def percentile(p: float) -> float:
            return times[min(len(times)-1, int((len(times)-1)*p))] if times else 0
        return {'generations': len(rows), 'fallback_rate': sum(r['fallback_reason'] is not None for r in rows)/max(1,len(rows)),
                'repeat_angle_rate':(len(angles)-len(set(angles)))/max(1,len(angles)),
                'angle_diversity':len(set(angles))/max(1,len(angles)),
                'p50_ms': percentile(.5), 'p95_ms': percentile(.95),
                'ratings': {str(r['rating']): r['count'] for r in ratings}}
