"""CLI: python -m contracts <kind> <file>... ; exit 1 on any violation."""

import sys

from . import KINDS, check_files


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in KINDS:
        print(f"usage: python -m contracts <{'|'.join(KINDS)}> <file>...", file=sys.stderr)
        return 2
    failed = check_files(argv[0], argv[1:])
    for path, messages in failed.items():
        for message in messages:
            print(f"{path}: {message}")
    if not failed:
        print(f"{len(argv) - 1} file(s) valid against {argv[0]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
