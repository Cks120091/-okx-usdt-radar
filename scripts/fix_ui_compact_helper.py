from pathlib import Path

p = Path('scripts/apply_ui_clarity_compact_patch.py')
text = p.read_text(encoding='utf-8')

old = 'h = history.read_text(encoding="utf-8")nh = h.replace('
new = 'h = history.read_text(encoding="utf-8")\nh = h.replace('
if old in text:
    text = text.replace(old, new, 1)

old_style = 'text = text.replace("</style>", compact_css + "  </style>", 1)'
new_style = 'text = text.replace("</style>", compact_css + "</style>", 1)'
if old_style not in text:
    raise SystemExit('style whitespace-fix anchor not found')
text = text.replace(old_style, new_style, 1)

p.write_text(text, encoding='utf-8')
