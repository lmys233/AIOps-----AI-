"""SDK and platform derive the same instance read credential without sending it in JSON."""
import hashlib,hmac


def pull_token(enrollment,identity):
    keys=('tenant','observed_id','service','instance','boot_id')
    text='sdk-pull-v1|'+'|'.join(identity[k] for k in keys)
    return hmac.new(enrollment.encode(),text.encode(),hashlib.sha256).hexdigest()
