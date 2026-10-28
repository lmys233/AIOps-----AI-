"""Operator entry point; command is never accepted through an AIOps tool."""
import argparse
import json
from pathlib import Path
from .lifecycle import Journal,LifecycleObserver


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--identity',required=True);parser.add_argument('--journal',required=True)
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args();command=args.command
    if command and command[0]=='--':command=command[1:]
    if not command:parser.error('child command required')
    identity=json.loads(Path(args.identity).read_text(encoding='utf-8'))
    return LifecycleObserver(identity,Journal(args.journal).write).run(command)


if __name__=='__main__':raise SystemExit(main())
