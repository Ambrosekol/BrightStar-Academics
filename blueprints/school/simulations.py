"""Settings > Your school > Learning simulations: the School Admin's or a head teacher's choices about the PhET
simulations students see (core/simulations.py has the rules, core/simulations_catalogue.py the list).

One page, three forms, each saving its own part of the one setting:

* **switches**: on or off for the whole school (on until someone turns it off), and class by class;
* **catalogue**: for each simulation (one topic at a time), shown or hidden, its first and last class, and in SSS
  the departments it is for;
* **reset**: every simulation back to the suggested classes and departments, and shown again.
"""

from flask import flash, redirect, render_template, request, url_for
from sqlalchemy import select

from app import app
from core import simulations as sims
from core.school_structure import DEPARTMENTS
from core.security import admin_access_error, admin_required, audit_log, csrf_protect, current_admin, holds_level, is_school_admin
from models import SchoolClass, db


def _may_change(me):
    """The School Admin, or a head teacher."""
    return bool(me and (is_school_admin(me) or holds_level(me['id'], 'head_teacher')))


def _classes():
    """The active classes from Primary 1 to SSS 3, in order: the ones a switch can be turned off for."""
    return db.session.scalars(select(SchoolClass).where(SchoolClass.active == 1,
                                                        SchoolClass.level_order.in_(list(sims.LEVELS)))
                              .order_by(SchoolClass.level_order, SchoolClass.name)).all()


def _save_switches(s):
    s['enabled'] = request.form.get('enabled') == '1'
    listed = {c.id for c in _classes()}
    turned_on = {int(v) for v in request.form.getlist('class_on') if v.isdigit()}
    # A class not on this page (inactive, say) keeps whatever it had.
    s['classes_off'] = sorted((set(s['classes_off']) - listed) | (listed - turned_on))
    return ('Simulations are on for students.' if s['enabled'] else 'Simulations are off for every student.'), \
        {'enabled': s['enabled'], 'classes_off': s['classes_off']}


def _save_catalogue(s):
    """Only the rows that were on the page (one topic's) are saved; a row back at its suggestion stores nothing."""
    hidden, changed = set(s['hidden']), []
    for slug in request.form.getlist('slug'):
        if slug not in sims.BY_SLUG:
            continue
        _slug, _title, _topic, lo, hi, departments = sims.BY_SLUG[slug]
        if request.form.get(f'show_{slug}') == '1':
            hidden.discard(slug)
        else:
            hidden.add(slug)
        new_lo = request.form.get(f'lo_{slug}', type=int)
        new_hi = request.form.get(f'hi_{slug}', type=int)
        if new_lo in sims.LEVELS and new_hi in sims.LEVELS:
            new_lo, new_hi = min(new_lo, new_hi), max(new_lo, new_hi)
            if (new_lo, new_hi) == (lo, hi):
                s['ranges'].pop(slug, None)
            else:
                s['ranges'][slug] = [new_lo, new_hi]
        # The departments only mean something for a simulation that reaches SSS.
        if max(new_hi or 0, hi) >= sims.FIRST_SENIOR and request.form.get(f'has_dept_{slug}') == '1':
            chosen = [d for d in DEPARTMENTS if d in request.form.getlist(f'dept_{slug}')]
            if chosen == [d for d in DEPARTMENTS if d in departments]:
                s['departments'].pop(slug, None)
            else:
                s['departments'][slug] = chosen
        changed.append(slug)
    s['hidden'] = sorted(hidden)
    return f'Saved {len(changed)} simulation{"" if len(changed) == 1 else "s"}.', {'saved': changed}


@app.route('/admin/school/simulations', methods=['GET', 'POST'])
@admin_required
@csrf_protect
def admin_school_simulations():
    me = current_admin()
    if not _may_change(me):
        return admin_access_error('Learning simulations')
    topic = request.values.get('topic', '')
    topic = topic if topic in sims.TOPICS else 'maths'
    if request.method == 'POST':
        s = sims.settings()
        action = request.form.get('action')
        if action == 'switches':
            message, detail = _save_switches(s)
        elif action == 'catalogue':
            message, detail = _save_catalogue(s)
        elif action == 'reset':
            s['ranges'], s['departments'], s['hidden'] = {}, {}, []
            message, detail = 'Every simulation is back to its suggested classes and shown again.', {'reset': True}
        else:
            return redirect(url_for('admin_school_simulations', topic=topic))
        sims.save(s, me['id'])
        audit_log('learning_simulations_saved', 'academics', 'school_setting', sims.KEY,
                  dict(detail, action=action), commit=False)
        db.session.commit()
        flash(message, 'success')
        return redirect(url_for('admin_school_simulations', topic=topic))
    s = sims.settings()
    rows = sims.entries(s)
    counts = {key: sum(1 for e in rows if e['topic'] == key) for key in sims.TOPICS}
    return render_template('admin_school_simulations.html', s=s, classes=_classes(), levels=sims.LEVELS,
                           first_senior=sims.FIRST_SENIOR, departments=DEPARTMENTS,
                           rows=[e for e in rows if e['topic'] == topic], topic=topic,
                           topics=[(key, label, counts[key]) for key, label in sims.TOPICS.items()],
                           changed=sum(1 for e in rows if e['changed'] or e['hidden']), total=len(rows),
                           about_url=sims.about_url, credit=sims.CREDIT)
