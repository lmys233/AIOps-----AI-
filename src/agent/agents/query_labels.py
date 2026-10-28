"""Literal service names encoded for RE2 inside a Go quoted query string."""
import json
import re


def service_pattern(services):
    # A hyphen is literal outside a character class. Python re.escape emits
    # escapes that Go string syntax rejects, so encode the regex separately.
    pattern='|'.join(re.sub(r'([\\.^$|?*+()\[\]{}])',r'\\\1',str(name)) for name in services)
    return json.dumps(pattern,ensure_ascii=False)[1:-1]
