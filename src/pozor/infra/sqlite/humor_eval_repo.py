import time
from pathlib import Path

from pozor.core.humor.evaluation import Comparison
from pozor.infra.sqlite.connection import connect


def pair(database: Path, chat: int, left: str, right: str, case: str) -> int:
    with connect(database) as db:
        valid=db.execute('SELECT 1 FROM chats WHERE chat_id=? AND allowed=1 AND enabled=1',(chat,)).fetchone()
        if not valid:
            raise ValueError('review chat must be enabled in this application')
        cursor=db.execute('INSERT INTO humor_evaluations(chat_id,left_variant,right_variant,case_id,created_at) VALUES (?,?,?,?,?)',
                          (chat,left,right,case,time.time()))
        return int(cursor.lastrowid or 0)


def sent(database: Path, pair_id: int, message: int) -> None:
    with connect(database) as db:
        db.execute('UPDATE humor_evaluations SET message_id=? WHERE id=?',(message,pair_id))


def vote(database: Path, pair_id: int, chat: int, message: int, user: int, choice: str) -> bool:
    if choice not in {'A','B','tie'}:
        return False
    with connect(database) as db:
        valid=db.execute('SELECT 1 FROM humor_evaluations e JOIN chats c ON c.chat_id=e.chat_id '
            'WHERE e.id=? AND e.chat_id=? AND e.message_id=? AND c.allowed=1 AND c.enabled=1',
            (pair_id,chat,message)).fetchone()
        if not valid:
            return False
        db.execute('INSERT INTO humor_votes VALUES (?,?,?) ON CONFLICT(evaluation_id,user_id) DO UPDATE SET choice=excluded.choice',
                   (pair_id,user,choice))
        return True


def results(database: Path, chat: int, variant: str = 'B') -> Comparison:
    wins=losses=ties=0
    with connect(database) as db:
        for row in db.execute('SELECT * FROM humor_evaluations WHERE chat_id=?',(chat,)).fetchall():
            counts={r['choice']:r['n'] for r in db.execute('SELECT choice,count(*) AS n FROM humor_votes WHERE evaluation_id=? GROUP BY choice',(row['id'],))}
            if not counts:
                continue
            left,right,draw=counts.get('A',0),counts.get('B',0),counts.get('tie',0)
            if left==right or draw>=max(left,right):
                ties+=1
            else:
                winner=row['left_variant'] if left>right else row['right_variant']
                if winner==variant:
                    wins+=1
                else:
                    losses+=1
    return Comparison(wins+losses+ties,wins,losses,ties)
