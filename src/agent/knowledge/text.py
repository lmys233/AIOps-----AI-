"""Chinese adjacent terms plus intact error/SQL identifiers; deterministic metadata."""
import re
import unicodedata


def clean(text):
    text=unicodedata.normalize('NFC',text).replace('\r\n','\n').replace('\r','\n')
    text=''.join(c for c in text if c in '\n\t' or unicodedata.category(c)!='Cc')
    return re.sub(r'\n{4,}','\n\n\n',text).strip()


def terms(text,limit=512):
    words=re.findall(r'\d{4}-\d{2}-\d{2}(?:T[\d:.+Z-]+)?|[A-Za-z0-9_]*[A-Za-z_][A-Za-z0-9_./:-]*|\d+',text)
    for word in re.findall(r'[\u3400-\u9fff]+',text):
        words.extend(word[i:i+2] for i in range(len(word)-1))
        if len(word)==1:words.append(word)
    return list(dict.fromkeys(w.lower() for w in words))[:limit]


def metadata(text):
    return {'entities':list(dict.fromkeys(re.findall(r'[A-Za-z0-9_]*[A-Za-z_][A-Za-z0-9_./:-]{1,100}',text)))[:60],
            'times':list(dict.fromkeys(re.findall(r'\d{4}-\d{2}-\d{2}(?:T[\d:.+Z-]+)?',text)))[:30],
            'code_terms':list(dict.fromkeys(re.findall(r'`([^`\n]{1,120})`',text)))[:40]}
