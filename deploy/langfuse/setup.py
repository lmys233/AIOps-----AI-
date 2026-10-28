"""Create private local credentials once; never change existing credentials."""
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / '.run' / 'langfuse'

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / 'langfuse.env'
    if path.exists():
        print('Existing local credentials preserved:', path)
        return
    values = {key: secrets.token_hex(24) for key in (
        'POSTGRES_PASSWORD', 'CLICKHOUSE_PASSWORD', 'MINIO_ROOT_PASSWORD',
        'REDIS_AUTH', 'SALT', 'NEXTAUTH_SECRET')}
    values.update(ENCRYPTION_KEY=secrets.token_hex(32),
        LANGFUSE_PUBLIC_KEY='pk-lf-'+secrets.token_hex(16),
        LANGFUSE_SECRET_KEY='sk-lf-'+secrets.token_hex(24),
        LANGFUSE_ADMIN_PASSWORD=secrets.token_urlsafe(24))
    with path.open('x', encoding='utf-8') as f:
        f.write('\n'.join(key+'='+value for key,value in values.items())+'\n')
    (OUT/'access.md').write_text(
        '# Local Langfuse access (private; do not commit)\n\n'
        'URL: http://127.0.0.1:3031\n\nUser: admin@aiops.local\n\n'
        'Password: '+values['LANGFUSE_ADMIN_PASSWORD']+'\n\n'
        'Project: AIOps Evaluation (aiops-evaluation)\n', encoding='utf-8')
    print('Created private credentials and login instructions in', OUT)

if __name__ == '__main__':
    main()
