"""Read-only source inventory and documentation checks; prints JSON to stdout."""
import ast
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import re
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent


def main():
    files = [ROOT / 'main.py']
    for folder in ('stalbot', 'tests', 'tools', 'dataset', 'benchmarks'):
        files.extend(sorted((ROOT / folder).glob('*.py')))
    inventory = {}
    for path in files:
        source = path.read_text(encoding='utf-8-sig')
        tree = ast.parse(source, filename=str(path))
        inventory[path.relative_to(ROOT).as_posix()] = {
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'lines': len(source.splitlines()),
            'lines_over_120': sum(len(line) > 120 for line in source.splitlines()),
            'semicolon_lines': sum(';' in line for line in source.splitlines()),
            'functions_over_80_lines': [
                {'name': node.name, 'line': node.lineno,
                 'lines': node.end_lineno - node.lineno + 1}
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.end_lineno - node.lineno + 1 > 80],
        }
    docs = [ROOT / 'README.md', *sorted((ROOT / 'docs').glob('*.md'))]
    missing, case_mismatch = [], []
    for path in docs:
        for target in re.findall(r'\]\(([^)]+)\)', path.read_text(encoding='utf-8-sig')):
            if target.startswith(('http:', 'https:', '#', 'mailto:')):
                continue
            target = unquote(target.split('#')[0].split('?')[0])
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                missing.append({'source': str(path.relative_to(ROOT)), 'target': target})
            elif resolved.is_file():
                actual = next(p.name for p in resolved.parent.iterdir()
                              if p.name.casefold() == resolved.name.casefold())
                if actual != resolved.name:
                    case_mismatch.append({'source': str(path.relative_to(ROOT)),
                                          'target': target, 'actual': actual})
    versions = {}
    for name in ('numpy', 'pillow', 'opencv-python-headless', 'opencv-python',
                 'rapidocr', 'onnxruntime-directml', 'tesserocr'):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    result = {'python': platform.python_version(), 'platform': platform.platform(),
              'versions': versions, 'parsed_python_files': len(inventory),
              'source': inventory, 'docs_missing_links': missing,
              'docs_case_mismatches': case_mismatch,
              'git_checkout_present': (ROOT / '.git').exists()}
    integration = OUT / 'integration-cpu.json'
    if integration.exists():
        data = json.loads(integration.read_text(encoding='utf-8-sig'))
        result['integration'] = {
            'rows': data['rows'], 'labels_sha256': data['labels_sha256'],
            'names': {batch: value['names_correct'] for batch, value in data['batches'].items()},
            'color_correct': data['color']['correct'],
            'numeric': {field: {key: value for key, value in metrics.items() if key != 'errors'}
                        for field, metrics in data['numeric'].items()},
            'cache_scanner_checks': len(data['name_cache']['scanner']),
            'cache_scanner_all_equal': all(item['equal'] for item in data['name_cache']['scanner']),
        }
        result['integration_source_hashes_match_current'] = all(
            hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected
            for relative, expected in data['provenance']['code_sha256'].items())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
