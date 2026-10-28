"""Derivable enrollment credentials; registry stores references, never bearer secrets."""
import hashlib,hmac,os,secrets
from pathlib import Path


class Credentials:
    def __init__(self,path=None,key=None):
        self.path=Path(path or os.getenv('AIOPS_OBSERVED_SIGNING_KEY_FILE') or
            Path(__file__).resolve().parents[3]/'.run/observed-registry.key')
        self.key=key

    def secret(self):
        if self.key is not None:return self.key
        self.path.parent.mkdir(parents=True,exist_ok=True)
        try:
            fd=os.open(self.path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        except FileExistsError:pass
        else:
            with os.fdopen(fd,'w',encoding='ascii') as f:f.write(secrets.token_hex(32))
        raw=self.path.read_text(encoding='ascii').strip()
        if len(raw)!=64:raise ValueError('invalid registry signing key')
        self.key=bytes.fromhex(raw)
        return self.key

    def token(self,grant_id):
        digest=hmac.new(self.secret(),grant_id.encode(),hashlib.sha256).hexdigest()
        return grant_id+'.'+digest

    def verify(self,value):
        if not isinstance(value,str) or len(value)!=101 or not value.startswith('enr_'):
            raise PermissionError('invalid enrollment credential')
        grant=value.split('.',1)[0]
        if not hmac.compare_digest(value,self.token(grant)):raise PermissionError('invalid enrollment credential')
        return grant


from diagnostics.credentials import pull_token
