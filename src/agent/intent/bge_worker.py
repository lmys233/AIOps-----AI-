"""Standalone BGE CPU worker. JSON-lines only on stdout; no Agent/SDK imports.

The encoding follows the user's media-judge ChineseTextEmbeddings implementation:
SentenceTransformer, batch_size=32, normalize_embeddings=True, device=cpu.
"""
import contextlib
import json
import sys


def main():
    with contextlib.redirect_stdout(sys.stderr):
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(sys.argv[1], device='cpu', local_files_only=True,
                                    revision=sys.argv[2] or None)
    for line in sys.stdin:
        try:
            texts = json.loads(line)
            if not isinstance(texts, list) or not 1 <= len(texts) <= 64:
                raise ValueError('invalid batch')
            if any(not isinstance(t, str) or len(t) > 4000 for t in texts):
                raise ValueError('invalid input')
            with contextlib.redirect_stdout(sys.stderr):
                vectors = model.encode(texts, batch_size=32, normalize_embeddings=True,
                                       show_progress_bar=False).tolist()
            print(json.dumps({'vectors': vectors}), flush=True)
        except Exception as exc:
            print(json.dumps({'error': type(exc).__name__}), flush=True)


if __name__ == '__main__':
    main()
