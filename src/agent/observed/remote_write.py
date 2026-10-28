"""Small PRW 1.0 encoder with Snappy literal blocks; no additional runtime package.

Protocol: https://prometheus.io/docs/specs/prw/remote_write_spec/
Used for low volume observation samples, not a replacement for business scraping.
"""
import math,re,struct


def varint(value):
    if value<0:raise ValueError('negative varint')
    out=bytearray()
    while value>=128:out.append((value&127)|128);value>>=7
    out.append(value);return bytes(out)


def field(number,value):
    return varint(number*8+2)+varint(len(value))+value


def snappy_literal(value):
    length=len(value)-1
    if length<0:return b'\x00'
    if length<60:tag=bytes([length<<2])
    else:
        size=max(1,(length.bit_length()+7)//8)
        tag=bytes([(59+size)<<2])+length.to_bytes(size,'little')
    return varint(len(value))+tag+value


def encode(samples):
    """samples: [(complete_labels, value, milliseconds)] in per-series time order."""
    out=bytearray()
    for labels,value,stamp in samples:
        if not math.isfinite(value) or type(stamp) is not int or stamp<0:raise ValueError('invalid metric sample')
        if not labels.get('__name__') or not re.fullmatch(r'[a-zA-Z_:][a-zA-Z0-9_:]*',labels['__name__']):raise ValueError('invalid metric name')
        series=bytearray()
        for name,text in sorted(labels.items()):
            if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]*',name) or not text:raise ValueError('invalid metric label')
            series.extend(field(1,field(1,name.encode())+field(2,str(text).encode())))
        series.extend(field(2,b'\x09'+struct.pack('<d',value)+b'\x10'+varint(stamp)))
        out.extend(field(1,bytes(series)))
    return snappy_literal(bytes(out))


async def send(client,url,samples):
    r=await client.post(url,content=encode(samples),headers={'Content-Encoding':'snappy',
        'Content-Type':'application/x-protobuf','User-Agent':'aiops-observer/1.0','X-Prometheus-Remote-Write-Version':'0.1.0'})
    r.raise_for_status();return r.status_code
