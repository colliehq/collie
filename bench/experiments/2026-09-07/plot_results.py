import json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from datetime import datetime, timezone, timedelta

DEST=ROOT
data=json.loads((DEST/'results.json').read_text(encoding='utf-8'))
plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'axes.spines.top':False,
                     'axes.spines.right':False,'figure.facecolor':'white','savefig.facecolor':'white'})
names={'durable-inbox-lease-revision-v2':'Durable inbox',
       'layercfg-provenance-merge-v2':'Layered config',
       'nscache-stale-reads-and-snapshots':'Cache + snapshots',
       'patchkit-transactional-directory-patch':'Transactional patch',
       'local-audit-request-id-v1':'Audit request ID',
       'local-circuit-breaker-v1':'Circuit breaker'}
native=data['primary_native_summary']; keys=sorted({r['task'] for r in native})
fig,axes=plt.subplots(1,2,figsize=(12,4.8),layout='constrained')
for ai,arm in enumerate(('claude-code','collie')):
    rows={r['task']:r for r in native if r['arm']==arm}
    positions=[i+(ai-.5)*.34 for i in range(len(keys))]
    for ax,field,scale in ((axes[0],'median_seconds',1),(axes[1],'median_cache_write_tokens',1000)):
        bars=ax.barh(positions,[rows[k][field]/scale for k in keys],height=.31,
                     color=('#667085','#167d8d')[ai],label=('Claude Code','Collie')[ai])
        ax.bar_label(bars,fmt='%.0f',padding=3,fontsize=9)
for ax in axes:
    ax.set_yticks(range(len(keys)),[names[k] for k in keys]);ax.invert_yaxis()
    ax.grid(axis='x',alpha=.18);ax.set_axisbelow(True);ax.margins(x=.14)
axes[0].set_title('Median elapsed time');axes[0].set_xlabel('Seconds (all attempts)')
axes[1].set_title('Median cache creation');axes[1].set_xlabel('Thousands of tokens (not billed dollars)')
fig.legend(*axes[0].get_legend_handles_labels(),loc='outside lower center',ncol=2,frameon=False)
fig.suptitle('Restricted native product track | Opus 5, high effort | n = 3 per task and arm',fontsize=13)
fig.savefig(DEST/'native-comparison.png',dpi=160)
plt.close(fig)

norm=data['primary_normalized_summary'];keys=sorted({r['task'] for r in norm})
fig,ax=plt.subplots(figsize=(11,5.5),layout='constrained')
for ai,arm in enumerate(('collie','hermes','pi','prime')):
    rows={r['task']:r for r in norm if r['arm']==arm}
    ax.barh([i+(ai-1.5)*.19 for i in range(len(keys))],[rows[k]['median_seconds'] for k in keys],
            height=.18,label=arm.title(),color=('#167d8d','#7c6bb0','#c78939','#667085')[ai])
ax.set_yticks(range(len(keys)),[names.get(k,k) for k in keys]);ax.invert_yaxis()
ax.set_xlabel('Median seconds (all attempts)');ax.grid(axis='x',alpha=.18);ax.set_axisbelow(True)
fig.legend(*ax.get_legend_handles_labels(),ncol=4,loc='outside lower center',frameon=False)
ax.set_title('Adapted harness track | All four arms: 7/9 correct, 9/9 clean\nSmall local taskset; differing tools; separate from native product results',pad=15)
fig.savefig(DEST/'normalized-comparison.png',dpi=160)
plt.close(fig)

quota=[q for q in data['quota'] if q.get('ok')]
pt=timezone(timedelta(hours=-7));times=[datetime.fromisoformat(q['at']).astimezone(pt) for q in quota]
fig,ax=plt.subplots(figsize=(10,4),layout='constrained')
for key,label,color in [('five_hour','5-hour window','#167d8d'),('seven_day','Weekly allowance','#667085')]:
    ax.plot(times,[q[key]['utilization'] for q in quota],marker='o',markersize=3,label=label,color=color)
import matplotlib.dates as mdates
ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M',tz=pt))
ax.set_ylim(0,105);ax.set_ylabel('Account utilization (%)');ax.set_xlabel('2026-09-07, Pacific daylight time')
ax.grid(alpha=.18);ax.legend(loc='upper left',frameon=False)
ax.set_title('Available window exhausted: 100% session / 39% weekly\nSession reset 15:50 PT; weekly reset 13:00 PT; paid extra usage disabled',pad=15)
fig.savefig(DEST/'quota-timeline.png',dpi=160)
plt.close(fig)
print('Rendered three benchmark figures.')
