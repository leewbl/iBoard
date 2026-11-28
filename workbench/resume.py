"""Resume a retained task using its original inputs and execution settings."""
import argparse
import json
import sys

from workbench.start import StartError, resume_task
from workbench import pickup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True)
    parser.add_argument('--inspect', action='store_true', help='Show the resume point without changing the task')
    args = parser.parse_args()
    if args.inspect:
        return pickup.main(['--task', args.task])
    print(json.dumps(resume_task(args.task), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except StartError as error:
        print(f'resume: {error}', file=sys.stderr)
        sys.exit(2)
