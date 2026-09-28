"""Command-line management of the platform: ``python -m control_plane <command>``.

    init                          create the platform registry tables
    create-platform-admin USER [--super]   add a platform admin (password prompted)
    create-tenant CODE "Name" [--domain HOST ...]   (the portal address is issued automatically)
    upgrade [CODE]                bring school database(s) up to the current schema (with no CODE it also
                                  records a new launch, like starting the server does)
    new-launch                    record a new launch of the server: everyone signed in must sign in
                                  again, except people in the middle of an exam
    drop-retired-tables [CODE] [--yes]   show (or with --yes drop) the removed website editor's leftover tables
    sweep-jobs [CODE]              give every school's stuck background jobs another try (run this on a
                                  cron/systemd timer; ordinary traffic already does it opportunistically,
                                  this is only needed for a school quiet enough that nothing else would)
    add-domain CODE HOST [--primary] / remove-domain HOST
    suspend CODE [--reason TEXT] / activate CODE
    create-db-role CODE [--rotate]  create (or, with --rotate, give a new password to) a PostgreSQL
                                  role that owns exactly this school's own database - prints the new
                                  connection URL, but changes nothing in the registry (see set-db-url)
    set-db-url CODE URL            point a school at a different connection string (a literal URL, or
                                  env:VARIABLE_NAME to read one from the environment instead)
    rotate-delivery-key OLD NEW [CODE]   re-encrypt every school's own mail/WhatsApp/Paystack secret
                                  from OLD to NEW - run this, then set BRIGHTSTARS_DELIVERY_KEY=NEW
                                  everywhere and restart; never change the environment variable first
    list

Database locations come from the environment: BRIGHTSTARS_PLATFORM_DB for the
registry and, per school, ``--db-url`` (a PostgreSQL URL; when omitted the school gets a database
of its own on the platform's server). See docs/architecture/MULTI_TENANCY.md.
"""

import argparse
import getpass
import os
import sys


def _parser():
    p = argparse.ArgumentParser(prog='python -m control_plane', description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='command', required=True)

    sub.add_parser('init')

    s = sub.add_parser('create-platform-admin')
    s.add_argument('username')
    s.add_argument('--display-name')
    s.add_argument('--super', dest='superadmin', action='store_true',
                   help='make this the (or another) super admin; the first admin is one automatically')

    s = sub.add_parser('create-tenant')
    s.add_argument('code')
    s.add_argument('name')
    s.add_argument('--domain', action='append', default=[],
                   help="A domain of the school's own, which reaches the portal once its DNS "
                        "points a CNAME at the portal address. The portal address itself is "
                        "always issued automatically.")
    s.add_argument('--db-url', help="PostgreSQL URL, or env:VARIABLE_NAME. Default: a database of the school's own on the platform's server.")
    s.add_argument('--db-schema', help='PostgreSQL schema for this school inside a shared database.')
    s.add_argument('--admin-username', help="Create the school's first admin; a temporary password is printed once.")
    s.add_argument('--admin-display-name')
    s.add_argument('--no-starter-banks', action='store_true',
                   help="Do not copy the platform's standard entrance question banks into the school.")

    s = sub.add_parser('upgrade')
    s.add_argument('code', nargs='?')

    sub.add_parser('new-launch')

    s = sub.add_parser('drop-retired-tables')
    s.add_argument('code', nargs='?')
    s.add_argument('--yes', action='store_true',
                   help='really drop them, including any that still hold rows. Without it nothing changes.')

    s = sub.add_parser('sweep-jobs')
    s.add_argument('code', nargs='?')

    s = sub.add_parser('add-domain')
    s.add_argument('code')
    s.add_argument('hostname')
    s.add_argument('--primary', action='store_true')

    s = sub.add_parser('remove-domain')
    s.add_argument('hostname')

    s = sub.add_parser('suspend')
    s.add_argument('code')
    s.add_argument('--reason')

    s = sub.add_parser('activate')
    s.add_argument('code')

    s = sub.add_parser('create-db-role')
    s.add_argument('code')
    s.add_argument('--rotate', action='store_true')

    s = sub.add_parser('set-db-url')
    s.add_argument('code')
    s.add_argument('url')

    s = sub.add_parser('rotate-delivery-key')
    s.add_argument('old')
    s.add_argument('new')
    s.add_argument('code', nargs='?')

    sub.add_parser('list')
    return p


def main(argv=None):
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
            role_name, url = pv.create_school_role(args.code, rotate=args.rotate)
            print(f'Role {role_name} {"rotated" if args.rotate else "created"} and now owns its database.')
            print(f'Connection URL (shown once): {url}')
            print('Set this as an environment variable on every worker, then point the school at it:')
            print(f'  set-db-url {args.code} env:YOUR_CHOSEN_VARIABLE_NAME')
        elif args.command == 'set-db-url':
            pv.set_db_url(args.code, args.url)
            print(f'{args.code} now uses this connection string on its next resolution.')
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
