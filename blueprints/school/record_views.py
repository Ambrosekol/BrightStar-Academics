"""Shaping attendance and exam-timetable rows for the pages families read.

The student and parent portals show the same two pages (templates/attendance_view.html and
templates/timetable_view.html). The data layers hand back plain rows; this turns them into what those
pages draw: attendance grouped by month and the days worth a second look, the timetable grouped by day
with the next paper picked out. Nothing here touches the database.
"""

from datetime import date, datetime

_STATUS_ORDER = ('absent', 'late', 'excused')


def _day(text):
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def _pretty(d):
    return f'{d.strftime("%a")} {d.day} {d.strftime("%b")}'


def attendance_view(history):
    """``history`` is newest first, as ``student_history`` returns it.

    Returns ``{'months': [...], 'notable': [...]}``. ``months`` runs oldest to newest, each with its
    recorded days (``day``, ``weekday``, ``status``, ``title``); ``notable`` is every day that was not a
    plain present, newest first, for the "days to note" list.
    """
    months, notable = {}, []
    for h in sorted(history, key=lambda r: str(r['date'])):
        d = _day(h['date'])
        if d is None:
            continue
        key = d.strftime('%Y-%m')
        month = months.setdefault(key, {'key': key, 'label': d.strftime('%B %Y'), 'days': [], 'counts': {}})
        month['days'].append({'day': d.day, 'weekday': d.strftime('%a'), 'status': h['status'],
                              'title': f'{_pretty(d)}: {h["status"].capitalize()}'})
        month['counts'][h['status']] = month['counts'].get(h['status'], 0) + 1
        if h['status'] != 'present':
            notable.append({'date': h['date'], 'pretty': f'{d.strftime("%A")} {d.day} {d.strftime("%B %Y")}',
                            'status': h['status'], 'class_name': h['class_name']})
    notable.sort(key=lambda r: str(r['date']), reverse=True)
    return {'months': list(months.values()), 'notable': notable}


def timetable_view(rows, today=None):
    """``rows`` is earliest first, as ``entry_rows`` returns it.

    Returns ``{'days': [...], 'next_up': entry or None, 'upcoming': n, 'done': n}``. Each day has its
    papers and whether it is today or already past; ``next_up`` is the first paper that has not yet
    finished, with how far away it is in words.
    """
    today = today or date.today()
    days, order = {}, []
    for r in rows:
        d = _day(r['date'])
        if d is None:
            continue
        if d not in days:
            days[d] = {'iso': d.isoformat(), 'weekday': d.strftime('%A'), 'day': d.day, 'month': d.strftime('%b'),
                       'year': d.year, 'past': d < today, 'today': d == today, 'entries': []}
            order.append(d)
        days[d]['entries'].append(r)
    out = [days[d] for d in sorted(order)]
    next_up = None
    for day in out:
        if not day['past'] and day['entries']:
            entry = day['entries'][0]
            gap = (date.fromisoformat(day['iso']) - today).days
            when = 'today' if gap == 0 else 'tomorrow' if gap == 1 else f'in {gap} days'
            next_up = {**entry, 'weekday': day['weekday'], 'day': day['day'], 'month': day['month'], 'when': when}
            break
    return {'days': out, 'next_up': next_up,
            'upcoming': sum(len(d['entries']) for d in out if not d['past']),
            'done': sum(len(d['entries']) for d in out if d['past'])}
