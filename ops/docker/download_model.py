"""Download a public, revision-pinned GGUF and verify before image publication."""

import hashlib
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REVISION = "7c41481f57cb95916b40956ab2f0b139b296d974"
SHA256 = "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785"
MODEL = "Qwen3-8B"
SIZE = 5027783488
if os.environ.get('MODEL_VARIANT', '8b') == '14b':
    MODEL = 'Qwen3-14B'
    REVISION = '530227a7d994db8eca5ab5ced2fb692b614357fd'
    SHA256 = '500a8806e85ee9c83f3ae08420295592451379b4f8cf2d0f41c15dffeb6b81f0'
    SIZE = 9001752960
elif os.environ.get('MODEL_VARIANT', '8b') != '8b':
    raise ValueError('Unsupported pinned model variant')
URL = f"https://huggingface.co/Qwen/{MODEL}-GGUF/resolve/{REVISION}/{MODEL}-Q4_K_M.gguf"


def download() -> None:
    temporary = Path("model.partial")
    try:
        digest = hashlib.sha256()
        with urllib.request.urlopen(URL, timeout=120) as response, temporary.open("wb") as output:
            while chunk := response.read(8 * 1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != SHA256:
            raise ValueError("Model checksum mismatch")
        temporary.replace("model.gguf")
    finally:
        temporary.unlink(missing_ok=True)


def download_parallel(*, chunk_size: int = 32 * 1024 * 1024, workers: int = 8) -> None:
    """Fetch verified HTTP ranges; publish only after whole-file SHA256 validation."""
    temporary = Path('model.partial')

    def part(start: int) -> int:
        end = min(SIZE, start + chunk_size) - 1
        request = urllib.request.Request(URL, headers={'Range': f'bytes={start}-{end}'})
        with urllib.request.urlopen(request, timeout=120) as response:
            if response.status != 206 or response.headers.get('Content-Range') != f'bytes {start}-{end}/{SIZE}':
                raise ValueError('Invalid model range response')
            data = response.read(end - start + 2)
        if len(data) != end - start + 1:
            raise ValueError('Incomplete model range')
        with temporary.open('r+b') as output:
            output.seek(start)
            output.write(data)
        return len(data)

    try:
        with temporary.open('wb') as output:
            output.truncate(SIZE)
        received = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            jobs = [executor.submit(part, start) for start in range(0, SIZE, chunk_size)]
            for job in as_completed(jobs):
                received += job.result()
                if received // (512 * 1024 * 1024) != (received - chunk_size) // (512 * 1024 * 1024):
                    print(f'Model download: {received // (1024 * 1024)} MiB', flush=True)
        with temporary.open('rb') as source:
            if hashlib.file_digest(source, 'sha256').hexdigest() != SHA256:
                raise ValueError('Model checksum mismatch')
        temporary.replace('model.gguf')
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    download_parallel()
