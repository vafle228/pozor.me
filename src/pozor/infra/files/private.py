"""Atomic private writes and platform-appropriate access control."""

import getpass
import os
import subprocess
import sys
import uuid
from pathlib import Path


def protect(path: Path, *, directory: bool = False) -> None:
    if sys.platform=='win32':
        flags='(OI)(CI)F' if directory else 'F'
        subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r',
            f'{getpass.getuser()}:{flags}',f'SYSTEM:{flags}'],check=True,capture_output=True,timeout=10)
    else:
        path.chmod(0o700 if directory else 0o600)


def private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary=path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        protect(temporary)
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)
