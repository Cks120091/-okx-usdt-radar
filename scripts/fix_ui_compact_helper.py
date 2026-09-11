from pathlib import Path

p = Path('scripts/apply_ui_clarity_compact_patch.py')
text = p.read_text(encoding='utf-8')
old = 'h = history.read_text(encoding="utf-8")nh = h.replace('
new = 'h = history.read_text(encoding="utf-8")\nh = h.replace('
if old not in text:
    raise SystemExit('syntax-fix anchor not found')
p.write_text(text.replace(old, new, 1), encoding='utf-8')
