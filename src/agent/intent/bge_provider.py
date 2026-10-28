"""Isolate BGE model/dependencies in a long-lived, configured Python worker."""
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from .catalog_models import digest
from .vector_provider import validate


def local_model(model, revision):
    path = Path(model)
    if not path.is_dir() and '/' in model:
        hub = Path(os.getenv('HF_HUB_CACHE') or str(Path(os.getenv('HF_HOME') or str(Path.home()/'.cache/huggingface'))/'hub'))
        root = hub / ('models--' + model.replace('/', '--'))
        ref = root/'refs'/ (revision or 'main')
        commit = ref.read_text().strip() if ref.is_file() else revision
        if commit and (root/'snapshots'/commit).is_dir():
            path = root/'snapshots'/commit
    if not path.is_dir():
        return model, digest([model, revision, 'cache-not-resolved'])
    path = path.resolve()
    # Pin the actual local snapshot, and invalidate if locally replaced weights/config
    # change. No full weight-file hashing/loading in the Agent request process.
    files = sorted(set(path.glob('*.json')) | set(path.glob('*.safetensors')) | set(path.glob('*.bin')))
    stamp = [(f.name, f.stat().st_size, f.stat().st_mtime_ns) for f in files]
    return str(path), digest([str(path), revision, stamp])


class BGEVectors:
    kind = 'semantic'

    def __init__(self, model='BAAI/bge-small-zh-v1.5', python=None, revision=''):
        self.model, fingerprint = local_model(model, revision)
        self.python, self.revision = python or sys.executable, revision
        self.identity = 'bge-cpu-normalized-v1-' + fingerprint
        self.process = None
        self.lock = asyncio.Lock()
        self.retry_at = 0

    async def start(self):
        if self.process and self.process.returncode is None:
            return
        import time
        if time.monotonic() < self.retry_at:
            raise RuntimeError('BGE_WORKER_BACKOFF')
        self.process = await asyncio.create_subprocess_exec(self.python, '-u',
            str(Path(__file__).with_name('bge_worker.py')), self.model, self.revision,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=2_000_000,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

    async def embed(self, texts):
        import json
        import time
        async with self.lock:
            try:
                await self.start()
                self.process.stdin.write((json.dumps(texts, ensure_ascii=False)+'\n').encode())
                await self.process.stdin.drain()
                raw = await self.process.stdout.readline()
                if not raw:
                    raise RuntimeError('BGE_WORKER_UNAVAILABLE')
                result = json.loads(raw)
                if result.get('error'):
                    raise RuntimeError('BGE_ENCODING_FAILED')
                vectors = [validate(v) for v in result['vectors']]
                if len(vectors) != len(texts) or len({len(v) for v in vectors}) != 1:
                    raise ValueError('BGE response mismatch')
                return vectors
            except BaseException:
                await self.close()
                self.retry_at = time.monotonic() + 5
                raise

    async def warm(self):
        # Stored examples alone cannot prove that this worker can currently encode
        # a new input. Load the model before marking background precomputation done.
        await self.embed(['初始化意图向量模型'])

    async def close(self):
        if self.process:
            if self.process.returncode is None:
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
            await self.process.wait()
            self.process = None
