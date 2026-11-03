"""Discover Core-owned migrations across feature persistence modules."""
import re


def core_migrations(core_path):
    files = list(core_path.glob('*/adapter/persistence/src/main/resources/db/migration/V*__*.sql'))
    versions = {}
    for path in files:
        match = re.fullmatch(r'V([0-9]+)__[a-z0-9_]+\.sql', path.name)
        if not match:
            raise ValueError('Invalid Core migration filename')
        version = int(match[1])
        if version in versions:
            raise ValueError('Duplicate Core migration version')
        versions[version] = path
    if not versions:
        raise ValueError('Core migration set is empty')
    return [versions[v] for v in sorted(versions)]
