import hashlib
import time
from datetime import UTC, datetime

from pozor.config import Settings
from pozor.core.domain.generation import GeneratedRoast, GenerationResult
from pozor.infra.sqlite.commands import start_session
from pozor.infra.sqlite.connection import connect, migrate
from pozor.infra.sqlite.generation_repo import GenerationRepository
from pozor.infra.sqlite.privacy_repo import forget
from pozor.infra.sqlite.retrieval import retrieve


def journal(tmp_path):
    database=tmp_path/'journal.sqlite3'
    migrate(database)
    text='Поставил семь будильников и снова проспал созвон'
    with connect(database) as db:
        db.execute('INSERT INTO chats(chat_id,allowed,enabled) VALUES (-1,1,1)')
        db.execute('INSERT INTO members(chat_id,user_id,consent) VALUES (-1,42,1),(-1,43,1),(-1,44,1)')
        db.execute("INSERT INTO messages(id,scope_chat_id,source_namespace,source_message_id,author_id,text,text_hash,occurred_at,origin) VALUES (1,-1,'live:-1',1,42,?,?,?,'live')",
                   (text,hashlib.sha256(text.encode()).hexdigest(),datetime.now(UTC).isoformat()))
        db.execute("INSERT INTO message_subjects VALUES (1,42,'self','observed_self')")
    sid=start_session(database,chat_id=-1,thread_id=0,target_id=42,initiator_id=43,trigger_message_id=99)
    pack=retrieve(database,chat_id=-1,thread_id=0,target_id=42,trigger_id=99,hint='Проспал созвон',target_label='Участник')
    repo=GenerationRepository(database)
    result=GenerationResult(roast=GeneratedRoast(angle='профсоюз будильников',text='Будильники уже создали профсоюз.',evidence_ids=['message:1']),fallback=False,prompt_version='v2')
    gid=repo.record(pack,result,Settings(_env_file=None,database_path=database),sid,1)
    with connect(database) as db:
        db.execute("INSERT INTO turns(session_id,turn_no,revision,status) SELECT id,1,revision,'sent' FROM sessions WHERE id=?",(sid,))
        db.execute("INSERT INTO outbox(session_id,turn_no,status,next_attempt_at,telegram_message_id) VALUES (?,1,'sent',?,999)",(sid,time.time()))
    return database,repo,gid,pack,result,sid


def test_ratings_are_bound_to_scope_message_and_participating_user(tmp_path):
    database,repo,gid,*_=journal(tmp_path)
    assert repo.rate(gid,chat=-1,thread=0,user=43,message_id=999,rating=1)
    assert repo.rate(gid,chat=-1,thread=0,user=44,message_id=999,rating=1)
    assert not repo.rate(gid,chat=-2,thread=0,user=43,message_id=999,rating=1)
    assert not repo.rate(gid,chat=-1,thread=0,user=777,message_id=999,rating=1)
    assert not repo.rate(gid,chat=-1,thread=0,user=43,message_id=998,rating=1)
    angles,answers,_,examples=repo.memory(-1,42)
    assert angles==['профсоюз будильников'] and answers
    assert len(examples)==2
    forget(database,-1,42)
    with connect(database) as db:
        assert db.execute('SELECT count(*) FROM generations').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM joke_ratings').fetchone()[0]==0


def test_source_edit_erases_private_generation_copy(tmp_path):
    database,repo,_,pack,result,sid=journal(tmp_path)
    with connect(database) as db:
        db.execute("UPDATE messages SET text='изменено',text_hash='new' WHERE id=1")
    assert repo.memory(-1,42)==([],[],set(),[])
    # A stale in-flight snapshot must not resurrect the edited text.
    # Hashes in entitlement alone aren't enough; the journal validates its snapshot.
    assert repo.record(pack,result,Settings(_env_file=None,database_path=database),sid,1) is None
