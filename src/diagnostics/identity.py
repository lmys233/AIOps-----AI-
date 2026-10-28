import hashlib


def database_identity(row):
    row=dict(row)
    host=row.pop('server_address',None);port=row.pop('server_port',None)
    if host is not None:
        value=f'{host}:{port}:{row.get("database","")}'
        row['server_id']=hashlib.sha256(value.encode()).hexdigest()[:24]
    return row
