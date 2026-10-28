"""Provider extensions restricted to reasoning knobs, never authority or tools."""
import copy,json
from urllib.parse import urlsplit


def validate(options):
    if not isinstance(options,dict) or set(options)-{'thinking','reasoning','reasoning_effort'}:raise ValueError('unsupported role model options')
    for key,value in options.items():
        if key=='thinking':
            if not isinstance(value,dict) or set(value)!={'type'} or value['type'] not in ('enabled','disabled'):raise ValueError('invalid thinking option')
        elif key=='reasoning':
            if not isinstance(value,dict) or set(value)!={'effort'} or value['effort'] not in ('none','minimal','low','medium','high','xhigh','max','ultra'):raise ValueError('invalid reasoning option')
        elif value not in ('none','minimal','low','medium','high','xhigh','max','ultra'):raise ValueError('invalid reasoning_effort')
    return copy.deepcopy(options)


def command_options(base_url,configured):
    # Provider protocol adapter; no assumption about a model name or observed system.
    if configured is not None and configured.strip():return validate(json.loads(configured))
    return {'thinking':{'type':'disabled'}} if urlsplit(base_url).hostname=='api.deepseek.com' else {}


def attribution_options(base_url,configured):
    """Proposal serialization has its own reasoning budget, never verdict authority.

    DeepSeek documents an enabled/high default. Bounded JSON proposals use its
    explicit non-thinking switch; other providers keep their own defaults.
    """
    return command_options(base_url,configured)
