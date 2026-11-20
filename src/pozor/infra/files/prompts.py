import hashlib
from pathlib import Path


class FilePromptStore:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or Path(__file__).resolve().parents[2] / 'prompts'

    def load(self, name: str) -> str:
        if name not in {'roast', 'dialogue'}:
            raise ValueError('unknown prompt')
        return (self.directory / f'{name}_system.txt').read_text('utf-8')

    def version(self, name: str) -> str:
        return 'v2:' + hashlib.sha256(self.load(name).encode()).hexdigest()[:16]
