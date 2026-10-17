"""Command-line management of the platform: ``python -m control_plane <command>``.

Each command is described in ``COMMANDS`` below: its group, a one-line summary (the list shown by
``python -m control_plane`` or ``--help``), a longer description and examples (shown by
``python -m control_plane <command> --help``). The parser is built from that table, so the help can
never fall out of step with the commands. Database locations come from the environment:
BRIGHTSTARS_PLATFORM_DB for the registry and, per school, ``--db-url``. See
docs/architecture/MULTI_TENANCY.md.
"""

import argparse
import getpass
import os
import sys

PROG = 'python -m control_plane'

# (group, name, summary, description, examples, arguments)
# Each argument: (flags, kwargs) for add_argument.
GROUPS = (
    ('setup', 'First-time setup'),
    ('schools', 'Schools'),
    ('maintenance', 'Upgrades and upkeep'),
    ('database', 'Databases'),
    ('security', 'Keys and secrets'),
    ('leaving', 'Exporting and removing a school'),
)

COMMANDS = [
    ('setup', 'init', 'Create the platform registry tables (once, on a new server)',
     """Creates the platform's own tables in the registry database (BRIGHTSTARS_PLATFORM_DB): the list
of schools, their addresses, the platform admins and the audit log. Safe to run again: anything already
there is left as it is. Every other command does this for itself, so it is only needed on its own when
you want to check the registry database can be reached.""",
     [f'{PROG} init'], []),

    ('setup', 'create-platform-admin', 'Add someone who can sign in to the platform console',
     """Adds a platform admin: someone who signs in to the platform console to create and manage schools.
The password is asked for on screen (never typed on the command line, where it would be kept in the
shell's history). The first admin ever created is a super admin automatically.""",
     [f'{PROG} create-platform-admin ambrose --display-name "Ambrose"',
      f'{PROG} create-platform-admin ada --super'],
     [(('username',), dict(help='the name they sign in with')),
      (('--display-name',), dict(help='the name shown in the console (default: the username)')),
      (('--super',), dict(dest='superadmin', action='store_true',
                          help='make them a super admin, who can also manage other platform admins'))]),

    ('schools', 'list', 'List every school: its code, status, addresses and database',
     """Shows every school on the platform with its code, whether it is active or suspended, and its
addresses (the portal address and any domain of its own) and where its database is (passwords hidden).""",
     [f'{PROG} list'], []),

    ('schools', 'create-tenant', 'Create a new school: its database, its folder and its portal address',
     """Registers a new school and builds everything it needs: its own database (with every table and the
standard classes and roles), its own folder of files, the standard entrance question banks, and its portal
address (CODE.<BRIGHTSTARS_PORTAL_DOMAIN>), which works at once. With --admin-username it also creates the
school's first School Admin and prints their one-time password, shown once.

CODE is the school's short code (letters, numbers and dashes, such as "greenfield" or "unity"): it becomes
part of the portal address and cannot be changed later.""",
     [f'{PROG} create-tenant greenfield "Greenfield College" --admin-username admin',
      f'{PROG} create-tenant unity "Unity Secondary School" --domain portal.unityschool.edu.ng --admin-username principal'],
     [(('code',), dict(help='the school\'s short code: part of its portal address, never changed later')),
      (('name',), dict(help='the school\'s full name, in quotes')),
      (('--domain',), dict(action='append', default=[], metavar='HOST',
                           help="a domain of the school's own (repeat for more). It reaches the portal once its DNS "
                                "points a CNAME at the portal address")),
      (('--db-url',), dict(help="where its database lives: a PostgreSQL URL, or env:VARIABLE_NAME. "
                                "Default: a database of its own on the platform's server")),
      (('--db-schema',), dict(help='keep the school as a schema inside a shared database (not with PgBouncer)')),
      (('--admin-username',), dict(help="create the school's first School Admin; their one-time password is printed once")),
      (('--admin-display-name',), dict(help="that admin's name as shown (default: the username)")),
      (('--no-starter-banks',), dict(action='store_true',
                                     help="do not copy the platform's standard entrance question banks into the school"))]),

    ('schools', 'add-domain', "Give a school a domain of its own",
     """Adds an address that reaches a school's portal. Point the domain's DNS at the portal address with a
CNAME record first. --primary marks it as the school's main address in the platform console.""",
     [f'{PROG} add-domain greenfield portal.greenfield.edu.ng --primary'],
     [(('code',), dict(help="the school's code")),
      (('hostname',), dict(help='the domain, such as portal.school.edu.ng (no https://)')),
      (('--primary',), dict(action='store_true', help="mark it as the school's main address in the console"))]),

    ('schools', 'remove-domain', "Remove one of a school's own domains",
     """Stops a domain reaching its school. The school's portal address (CODE.<portal domain>) cannot be
removed: it always works.""",
     [f'{PROG} remove-domain portal.greenfield.edu.ng'],
     [(('hostname',), dict(help='the domain to remove'))]),

    ('schools', 'suspend', "Take a school's portal offline (its data is kept)",
     """Takes a school's portal offline for everyone: staff, parents and students see a notice instead.
Nothing is deleted; activate brings it back exactly as it was. Takes effect within
BRIGHTSTARS_REGISTRY_CACHE_SECONDS on every server.""",
     [f'{PROG} suspend greenfield --reason "Subscription unpaid"'],
     [(('code',), dict(help="the school's code")),
      (('--reason',), dict(help='why, kept in the platform audit log'))]),

    ('schools', 'activate', 'Bring a suspended school back online',
     """Reopens a suspended school's portal, with all its data as it was.""",
     [f'{PROG} activate greenfield'],
     [(('code',), dict(help="the school's code"))]),

    ('maintenance', 'upgrade', "Bring school databases up to date after a deploy (run on every release)",
     """Adds any new tables and columns the new version of the application needs, to one school (CODE) or
to every school. Run it as a deploy step, before the new version starts serving. With no CODE it also
records a new launch, so everyone signed in signs in again (people in the middle of an exam are left alone).
Safe to run more than once.""",
     [f'{PROG} upgrade', f'{PROG} upgrade greenfield'],
     [(('code',), dict(nargs='?', help='one school only (default: every school)'))]),

    ('maintenance', 'new-launch', 'Sign everyone out (except people sitting an exam)',
     """Records a new launch of the server: everyone signed in to any school must sign in again, except
students and candidates in the middle of an exam. upgrade (with no CODE) does this too; use this on its
own after a security change, such as a changed secret.""",
     [f'{PROG} new-launch'], []),

    ('maintenance', 'sweep-jobs', "Retry stuck background jobs and send due birthday texts (run on a timer)",
     """Gives every school's stuck background work another try: emails and texts that failed, exports
that stopped, webhooks to resend. Ordinary traffic already does this; a school quiet enough for nothing
else to happen needs this. It also sends the day's birthday texts to parents, so a weekend or holiday
birthday is not missed. Run it at least hourly in the daytime from cron or a systemd timer.""",
     [f'{PROG} sweep-jobs', f'{PROG} sweep-jobs greenfield',
      '# crontab, every 30 minutes:  */30 * * * *  cd /srv/brightstars && venv/bin/python -m control_plane sweep-jobs'],
     [(('code',), dict(nargs='?', help='one school only (default: every school)'))]),

    ('maintenance', 'drop-retired-tables', "Show (or remove) tables left behind by removed features",
     """Lists tables in each school's database that the application no longer uses (from features that
were removed), with how many rows each holds. Empty ones are already dropped by upgrade. Nothing changes
unless you add --yes.""",
     [f'{PROG} drop-retired-tables', f'{PROG} drop-retired-tables greenfield --yes'],
     [(('code',), dict(nargs='?', help='one school only (default: every school)')),
      (('--yes',), dict(action='store_true',
                        help='really drop them, including any that still hold rows. Without it nothing changes'))]),

    ('database', 'create-db-role', "Give a school its own database login, owning only its database",
     """Creates a PostgreSQL role that owns exactly one school's database and nothing else, so a leak
reaches only that school. Prints the new connection URL once, but changes nothing yet: put the URL in an
environment variable on every server, then point the school at it with set-db-url. --rotate gives an
existing role a new password the same way.""",
     [f'{PROG} create-db-role greenfield',
      f'{PROG} set-db-url greenfield env:GREENFIELD_DB_URL      # after setting GREENFIELD_DB_URL on every server',
      f'{PROG} create-db-role greenfield --rotate'],
     [(('code',), dict(help="the school's code")),
      (('--rotate',), dict(action='store_true', help='give the existing role a new password')),
      (('--password',), dict(help='choose the password yourself (at least 10 characters); default: generated'))]),

    ('database', 'set-db-url', "Point one school at a different database address",
     """Changes where one school's database is. URL is a full PostgreSQL URL, or env:VARIABLE_NAME to read
it from an environment variable (so the password is not stored in the registry). Takes effect on each
server once its registry cache expires.""",
     [f'{PROG} set-db-url greenfield env:GREENFIELD_DB_URL',
      f'{PROG} set-db-url greenfield "postgresql+psycopg://school_greenfield:...@db2:5432/brightstars_greenfield"'],
     [(('code',), dict(help="the school's code")),
      (('url',), dict(help='a PostgreSQL URL, or env:VARIABLE_NAME'))]),

    ('database', 'move-db-address', "Move every school from one database address to another (PgBouncer)",
     """Points every school whose database is at one host:port at another, keeping each school's database
name and login. For putting PgBouncer in front of a server that already has schools (5432 to 6432), or
moving to a new server. Without --apply it only shows what would change. Schools whose address is an
env: variable, or on another server, are listed and left alone.""",
     [f'{PROG} move-db-address --from 127.0.0.1:5432 --to 127.0.0.1:6432',
      f'{PROG} move-db-address --from 127.0.0.1:5432 --to 127.0.0.1:6432 --apply'],
     [(('--from',), dict(dest='from_address', required=True, metavar='HOST:PORT', help='the address the schools use now')),
      (('--to',), dict(dest='to_address', required=True, metavar='HOST:PORT', help='the address to use instead')),
      (('--apply',), dict(action='store_true', help='make the change (without it, nothing is changed)'))]),

    ('security', 'rotate-delivery-key', "Change the key that encrypts schools' email, SMS and Paystack secrets",
     """Re-encrypts every school's stored mail password, SMS token and Paystack keys from the OLD key to
the NEW one. Order matters: run this first, THEN set BRIGHTSTARS_DELIVERY_KEY=NEW on every server and
restart. Never change the environment variable first, or the stored secrets can no longer be read.""",
     [f'{PROG} rotate-delivery-key "$OLD_KEY" "$NEW_KEY"'],
     [(('old',), dict(help='the key in use now')),
      (('new',), dict(help='the new key')),
      (('code',), dict(nargs='?', help='one school only (default: every school)'))]),

    ('leaving', 'export-tenant', "Save a school's whole database and files into a folder",
     """Writes everything one school owns into a new folder: its whole database (database.sql, made with
pg_dump) and its uploaded files, with a README on how to bring it back. What a school leaving the
platform is owed. Needs pg_dump on this machine (or BRIGHTSTARS_PG_DUMP).""",
     [f'{PROG} export-tenant greenfield --output /backups'],
     [(('code',), dict(help="the school's code")),
      (('--output',), dict(default='.', metavar='DIR', help='where to create the export folder (default: here)'))]),

    ('leaving', 'delete-tenant', "Export a school, then remove it permanently",
     """Permanently removes a school: its database, its files and its place in the registry, after first
exporting it into DIR (see export-tenant). Cannot be undone; the export is the only way back.""",
     [f'{PROG} delete-tenant oldschool --output /backups'],
     [(('code',), dict(help="the school's code")),
      (('--output',), dict(default='.', metavar='DIR', help='where to create the backup export folder (default: here)'))]),

    ('leaving', 'force-delete-tenant', "Remove a school permanently, with NO export first",
     """The same permanent removal as delete-tenant, with no export taken first. Only for a school already
exported, or a test school that was never real. Cannot be undone.""",
     [f'{PROG} force-delete-tenant test-school'],
     [(('code',), dict(help="the school's code"))]),
]


def overview():
    """The grouped list of commands shown by ``python -m control_plane`` and ``--help``."""
    width = max(len(name) for _, name, *_ in COMMANDS) + 2
    lines = ['Manage the Brightstars Academics platform from the server.', '',
             f'Usage:  {PROG} <command> [options]',
             f'        {PROG} <command> --help      what a command does, with examples', '']
    for key, title in GROUPS:
        lines.append(title.upper())
        for group, name, summary, *_ in COMMANDS:
            if group == key:
                lines.append(f'  {name:<{width}}{summary}')
        lines.append('')
    lines += ['Common jobs:',
              f'  New server:      {PROG} init  then  {PROG} create-platform-admin <you>',
              f'  New school:      {PROG} create-tenant <code> "<Name>" --admin-username admin',
              f'  Each release:    {PROG} upgrade',
              f'  On a timer:      {PROG} sweep-jobs', '',
              'Database locations come from the environment: BRIGHTSTARS_PLATFORM_DB for the registry; each',
              "school's own database is created beside it unless create-tenant is given --db-url."]
    return '\n'.join(lines)


class _Formatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, max_help_position=30, width=100)


def _parser():
    options = dict(prog=PROG, usage=f'{PROG} <command> [options]', description=overview(), formatter_class=_Formatter)
    try:
        p = argparse.ArgumentParser(suggest_on_error=True, **options)     # "did you mean 'upgrade'?" (Python 3.14)
    except TypeError:
        p = argparse.ArgumentParser(**options)
    # The commands are already listed, grouped, in the description: argparse's own list is not shown again.
    sub = p.add_subparsers(dest='command', required=True, metavar='<command>', help=argparse.SUPPRESS)
    for group, name, summary, description, examples, arguments in COMMANDS:
        epilog = 'Examples:\n' + '\n'.join(f'  {e}' for e in examples) if examples else None
        s = sub.add_parser(name, description=f'{summary}.\n\n{description}', epilog=epilog,
                           formatter_class=_Formatter)
        for flags, kwargs in arguments:
            s.add_argument(*flags, **kwargs)
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:
        print(overview())          # no command: the list of commands, not an error
        return 0
    args = _parser().parse_args(argv)
    from . import provisioning as pv
    from .registry import get_tenant, init_platform_db, platform_session, to_info

    try:
        if args.command == 'init':
            init_platform_db()
            print('Platform registry ready.')
        elif args.command == 'create-platform-admin':
            password = os.environ.get('BRIGHTSTARS_NEW_ADMIN_PASSWORD') or getpass.getpass('Password (min 10 chars): ')
            role = pv.create_platform_admin(args.username, args.display_name, password,
                                            superadmin=args.superadmin)
            print(f'Platform admin {args.username} created as '
                  f'{"the super admin" if role == "superadmin" else "a platform admin"}.')
        elif args.command == 'create-tenant':
            info, password = pv.create_tenant(
                args.code, args.name, args.domain, db_url=args.db_url, db_schema=args.db_schema,
                admin_username=args.admin_username, admin_display_name=args.admin_display_name,
                starter_banks=not args.no_starter_banks)
            portal, customs = pv.domains_of(info.slug)
            print(f'School {info.slug} portal: {portal}')
            for hostname in customs:
                print(f'  {hostname} reaches it once a CNAME points it at {portal}')
            if password:
                print(f'First admin: {args.admin_username}   temporary password (shown once): {password}')
        elif args.command == 'upgrade':
            # The registry first: a school's row is read with every column the code now declares.
            init_platform_db()
            if args.code:
                with platform_session() as session:
                    tenant = get_tenant(session, args.code)
                    if not tenant:
                        raise pv.ProvisioningError(f'No school with code "{args.code}".')
                    infos = [to_info(tenant)]
                for info in infos:
                    pv.upgrade_tenant(info)
            else:
                infos = pv.upgrade_all_tenants()
                # A deploy step that upgrades every school is the start of a new launch (see
                # control_plane/launch.py), so people signed in before it sign in again.
                from .launch import record_new_launch
                record_new_launch()
            print('Upgraded: ' + (', '.join(i.slug for i in infos) or 'nothing to upgrade'))
        elif args.command == 'new-launch':
            from .launch import record_new_launch

            record_new_launch()
            print('New launch recorded. Everyone signed in before now must sign in again, '
                  'except people in the middle of an exam.')
        elif args.command == 'drop-retired-tables':
            from core import retired_tables

            with platform_session() as session:
                if args.code:
                    tenant = get_tenant(session, args.code)
                    if not tenant:
                        raise pv.ProvisioningError(f'No school with code "{args.code}".')
                    infos = [to_info(tenant)]
                else:
                    infos = pv.list_tenant_infos()
            leftovers = 0
            if not infos:
                print('No schools registered.')
            for info in infos:
                held = retired_tables.for_school(info, drop=args.yes)
                leftovers += len(held)
                if not held:
                    print(f'{info.slug:<16} nothing left over')
                for table, rows in held.items():
                    print(f'{info.slug:<16} {table}: {rows} row(s)' + ('  -> dropped' if args.yes else ''))
                if held and args.yes:
                    pv.record('tenant.retired_tables_dropped', ', '.join(sorted(held)), tenant_id=info.id)
            if leftovers and not args.yes:
                print('Nothing was changed. Add --yes to drop these tables for good (their rows are lost).')
        elif args.command == 'sweep-jobs':
            from .context import tenant_context
            from core import jobs
            import app as A  # deferred: app.py registers every route at import time

            with platform_session() as session:
                if args.code:
                    tenant = get_tenant(session, args.code)
                    if not tenant:
                        raise pv.ProvisioningError(f'No school with code "{args.code}".')
                    infos = [to_info(tenant)]
                else:
                    infos = pv.list_tenant_infos()
            if not infos:
                print('No schools registered.')
            total = 0
            for info in infos:
                try:
                    with A.app.app_context(), tenant_context(info):
                        restarted = jobs.sweep_all_kinds()
                        from blueprints.school.birthdays import start_birthday_texts
                        if start_birthday_texts(inline=True):
                            print(f'{info.slug:<16} birthday texts sent')
                        from blueprints.finance.reminders import start_reminders
                        if start_reminders(inline=True):
                            print(f'{info.slug:<16} fee reminders sent')
                except Exception as exc:  # a school mid-upgrade, or unreachable, must not stop the rest
                    print(f'{info.slug:<16} could not be swept: {exc}')
                    continue
                total += restarted
                print(f'{info.slug:<16} {restarted} job(s) restarted' if restarted else f'{info.slug:<16} nothing stuck')
            print(f'Total: {total} job(s) restarted across {len(infos)} school(s).')
        elif args.command == 'add-domain':
            pv.add_domain(args.code, args.hostname, args.primary)
            print('Domain added.')
        elif args.command == 'remove-domain':
            pv.remove_domain(args.hostname)
            print('Domain removed.')
        elif args.command == 'suspend':
            pv.set_status(args.code, 'suspended', args.reason)
            print(f'{args.code} suspended.')
        elif args.command == 'activate':
            pv.set_status(args.code, 'active')
            print(f'{args.code} activated.')
        elif args.command == 'create-db-role':
            role_name, url = pv.create_school_role(args.code, rotate=args.rotate, password=args.password)
            print(f'Role {role_name} {"rotated" if args.rotate else "created"} and now owns its database.')
            print(f'Connection URL (shown once): {url}')
            print('Set this as an environment variable on every worker, then point the school at it:')
            print(f'  set-db-url {args.code} env:YOUR_CHOSEN_VARIABLE_NAME')
        elif args.command == 'set-db-url':
            pv.set_db_url(args.code, args.url)
            print(f'{args.code} now uses this connection string on its next resolution.')
        elif args.command == 'move-db-address':
            def address(text):
                host, _, port = text.rpartition(':')
                if not host or not port.isdigit():
                    raise SystemExit(f'Give an address as host:port, not {text!r}.')
                return host, int(port)
            (fh, fp), (th, tp) = address(args.from_address), address(args.to_address)
            moved, skipped = pv.move_db_address(fh, fp, th, tp, apply=args.apply)
            for slug, what in moved:
                print(f'  {"moved" if args.apply else "would move"}  {slug}: {what}')
            for slug, why in skipped:
                print(f'  left alone   {slug}: {why}')
            print(f'{len(moved)} school(s) {"moved" if args.apply else "would move"}, {len(skipped)} left alone.'
                  + ('' if args.apply else ' Nothing changed: add --apply to make the change.'))
        elif args.command == 'rotate-delivery-key':
            from .context import tenant_context
            from core.secrets_rotation import rotate_one_tenant
            import app as A  # deferred: app.py registers every route at import time

            with platform_session() as session:
                if args.code:
                    tenant = get_tenant(session, args.code)
                    if not tenant:
                        raise pv.ProvisioningError(f'No school with code "{args.code}".')
                    infos = [to_info(tenant)]
                else:
                    infos = pv.list_tenant_infos()
            if not infos:
                print('No schools registered.')
            for info in infos:
                try:
                    with A.app.app_context(), tenant_context(info):
                        rotated, skipped = rotate_one_tenant(args.old, args.new)
                except Exception as exc:
                    print(f'{info.slug:<16} could not be rotated: {exc}')
                    continue
                note = f', could not read: {", ".join(skipped)}' if skipped else ''
                print(f'{info.slug:<16} {rotated} secret(s) rotated{note}')
            print('Once every school above shows 0 unreadable, set BRIGHTSTARS_DELIVERY_KEY to NEW '
                  'everywhere and restart - not before.')
        elif args.command == 'export-tenant':
            path = pv.export_tenant_data(args.code, args.output)
            print(f'Exported {args.code} to {path}')
            print('  database.sql - a plain-SQL dump of the school\'s own database')
            print('  files/       - a copy of the school\'s own uploads folder')
            print('  README.txt   - what is here, and how to restore it')
        elif args.command == 'delete-tenant':
            path = pv.delete_tenant(args.code, backup=True, backup_dir=args.output)
            print(f'{args.code} exported to {path}, then permanently deleted: its database, its '
                  'files, and its place in the registry are gone.')
        elif args.command == 'force-delete-tenant':
            pv.delete_tenant(args.code, backup=False)
            print(f'{args.code} permanently deleted, with no backup taken: its database, its '
                  'files, and its place in the registry are gone.')
        elif args.command == 'list':
            rows = pv.list_tenants()
            for slug, name, status, portal, customs, url in rows:
                print(f'{slug:<16} {status:<10} {name}')
                print(f'    portal:   {portal or "-"}')
                print(f'    own:      {", ".join(customs) or "-"}')
                print(f'    database: {url}')
            if not rows:
                print('No schools registered.')
    except (pv.ProvisioningError, ValueError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
