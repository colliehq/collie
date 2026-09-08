"""Compare follow-up cache use; initial warm-up calls are excluded explicitly."""
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent
rows = [json.loads(path.read_text(encoding='utf-8'))
        for path in sorted((ROOT/'cache-real-dialogue').glob('*/result.json'))]
summaries = []
for mode in ('flat', 'session'):
    group = [row for row in rows if row['mode'] == mode]
    turns = [turn for row in group for turn in row['turns'] if turn['turn'] > 0]
    assert len(group) == 3 and len(turns) == 15
    writes = [turn['usage']['cache_creation_input_tokens'] for turn in turns]
    reads = [turn['usage']['cache_read_input_tokens'] for turn in turns]
    summaries.append({'mode': mode, 'repetitions': 3, 'followup_calls': len(turns),
                      'completed_calls_including_initial': sum(row['completed'] for row in group),
                      'followup_cache_hit_calls': sum(n > 0 for n in reads),
                      'followup_cache_write_total': sum(writes),
                      'followup_cache_read_total': sum(reads),
                      'followup_cache_write_median': statistics.median(writes),
                      'wall_seconds_median_including_initial': statistics.median(row['seconds'] for row in group)})
output = {'summary': summaries, 'followup_cache_write_reduction':
          1 - summaries[1]['followup_cache_write_total']/summaries[0]['followup_cache_write_total'],
          'claim': 'Supplied public-code review, not tool execution or task correctness. '
                   'First calls excluded from cache comparison because some were already warm. '
                   'Modes have the same first prompt but naturally different subsequent model output.'}
(ROOT/'cache-dialogue-summary.json').write_text(json.dumps(output, indent=2), encoding='utf-8')
print(json.dumps(output))
