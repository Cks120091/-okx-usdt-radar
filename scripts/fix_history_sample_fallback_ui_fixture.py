from pathlib import Path

p = Path('scripts/check_history_replay_ui.py')
text = p.read_text()
text = text.replace("case('missing-cohort',lambda d:d.update(groups={}),'同類情境樣本不足')", "case('missing-cohort',lambda d:d.update(groups={},symbol_groups={}),'同類情境樣本不足')")
text = text.replace("case('too-small',lambda d:d['groups'][key].update(resolved=5,wins=3,losses=2,total=5),'不足')", "case('too-small',lambda d:(d['groups'][key].update(resolved=5,wins=3,losses=2,total=5),d['symbol_groups'][item['inst_id']][key].update(resolved=5,wins=3,losses=2,total=5)),'不足')")
text = text.replace("case('unknown-symbol',lambda d:d.update(covered_inst_ids=[]),'此幣不在8支大型幣樣本／歷史不足')", "case('unknown-symbol',lambda d:d.update(covered_inst_ids=[]),'62.0%')")
p.write_text(text)
