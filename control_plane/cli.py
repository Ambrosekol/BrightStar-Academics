"""Command-line management of the platform: ``python -m control_plane <command>``.

    init                          create the platform registry tables
    create-platform-admin USER    add a platform admin (password prompted)
    adopt-superadmin --from-db F  copy an existing installation's Super Admin(s) into the platform
    create-tenant CODE "Name" [--domain HOST ...]   (the portal address is issued automatically)
    register-existing CODE "Name" --from-db cbt.db [--domain HOST --from-data data --from-uploads static/uploads]
    upgrade [CODE]                bring school database(s) up to the current schema
    add-domain CODE HOST [--primary] / remove-domain HOST
    suspend CODE [--reason TEXT] / activate CODE
    list

Database locations come from the environment: BRIGHTSTARS_PLATFORM_DB for the
registry and, per school, ``--db-url`` (PostgreSQL in production; SQLite files
under BRIGHTSTARS_TENANTS_DIR when omitted). See docs/architecture/MULTI_TENANCY.md.
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

    s = sub.add_parser('adopt-superadmin')
    s.add_argument('--from-db', required=True)
    s.add_argument('--username')

    s = sub.add_parser('create-tenant')
    s.add_argument('code')
    s.add_argument('name')
    s.add_argument('--domain', action='append', default=[],
                   help="A domain of the school's own, which reaches the portal once its DNS "
                        "points a CNAME at the portal address. The portal address itself is "
                        "always issued automatically.")
    s.add_argument('--db-url', help='SQLAlchemy URL, or env:VARIABLE_NAME. Default: a SQLite file under the tenants folder.')
    s.add_argument('--db-schema', help='PostgreSQL schema for this school inside a shared database.')
    s.add_argument('--admin-username', help="Create the school's first admin; a temporary password is printed once.")
    s.add_argument('--admin-display-name')

    s = sub.add_parser('register-existing')
    s.add_argument('code')
    s.add_argument('name')
    s.add_argument('--domain', action='append', default=[])
    s.add_argument('--from-db', required=True)
    s.add_argument('--from-data')
    s.add_argument('--from-uploads')

    s = sub.add_parser('upgrade')
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
            pv.create_platform_admin(args.username, args.display_name, password)
            print(f'Platform admin {args.username} created.')
        elif args.command == 'adopt-superadmin':
            adopted = pv.adopt_superadmins(args.from_db, args.username)
            print('Adopted: ' + (', '.join(adopted) if adopted else 'nothing new (already present)'))
        elif args.command == 'create-tenant':
            info, password = pv.create_tenant(
                args.code, args.name, args.domain, db_url=args.db_url, db_schema=args.db_schema,
                admin_username=args.admin_username, admin_display_name=args.admin_display_name)
            portal, customs = pv.domains_of(info.slug)
            print(f'School {info.slug} portal: {portal}')
            for hostname in customs:
                print(f'  {hostname} reaches it once a CNAME points it at {portal}')
            if password:
                print(f'First admin: {args.admin_username}   temporary password (shown once): {password}')
        elif args.command == 'register-existing':
            info = pv.register_existing_tenant(args.code, args.name, args.domain, args.from_db,
                                               args.from_data, args.from_uploads)
            portal, _ = pv.domains_of(info.slug)
            print(f'School {info.slug} registered from {args.from_db} (originals untouched).')
            print(f'  portal: {portal}')
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
            print('Upgraded: ' + (', '.join(i.slug for i in infos) or 'nothing to upgrade'))
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
