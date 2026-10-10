"""What every system-wide environment variable is for, so the platform's Settings page
(``control_plane/settings_console.py``) can show the same explanation ``.env.example`` and
``templates/platform/docs/reference-config.html`` already give a person reading the source, to
whoever is looking at it in the portal instead.

Nothing here reads or writes a value; it is just the catalogue. ``env_file.py`` reads and writes
``.env`` itself, guided by the ``name``\\ s declared here (it refuses to write a name that is not).

Each entry is deliberately plain data, not a class with behaviour: a Settings page row is built by
walking this list and asking ``env_file`` for the variable's current value, nothing more.
"""

from dataclasses import dataclass, field

RISK_LOW, RISK_MEDIUM, RISK_HIGH, RISK_CRITICAL = 'low', 'medium', 'high', 'critical'
RISK_ORDER = {RISK_LOW: 0, RISK_MEDIUM: 1, RISK_HIGH: 2, RISK_CRITICAL: 3}

EFFECT_IMMEDIATE, EFFECT_RESTART = 'immediate', 'restart'


@dataclass(frozen=True)
class Variable:
    name: str
    category: str
    risk: str
    purpose: str
    when_to_change: str
    affects: str
    breaks_if_wrong: str
    default: str = ''
    secret: bool = False
    effect: str = EFFECT_IMMEDIATE
    # A second env var name this one is read from as a fallback (e.g. FLASK_ENV for BRIGHTSTARS_ENV),
    # shown for context only - editing always writes the primary name.
    aliases: tuple = field(default_factory=tuple)


VARIABLES = [
    Variable(
        name='BRIGHTSTARS_PLATFORM_DB', category='Database', risk=RISK_CRITICAL, secret=True,
        effect=EFFECT_RESTART,
        purpose='The PostgreSQL URL of the platform registry: which schools exist, their addresses, '
                'and the platform admins. There is no default on purpose - a wrong guess would '
                'silently create a new, empty registry.',
        when_to_change='Moving the registry to a different PostgreSQL server, or changing the role it '
                       'connects with.',
        affects='Every school and every platform admin - this is the one database everything else is '
                'found through.',
        breaks_if_wrong='The console cannot find any school (and may create a second, empty registry '
                        'if a fresh database is reachable at the new URL). Get this exactly right, or '
                        'not at all.'),
    Variable(
        name='BRIGHTSTARS_SCHOOL_DB_TEMPLATE', category='Database', risk=RISK_CRITICAL,
        purpose="Where a new school's own database is created, when its creation does not say "
                'otherwise: a URL template with {slug} (the school code) and/or {db} (the full '
                'database name). Left blank, new schools go on the same server as the registry.',
        when_to_change='Moving schools to their own database server, separate from the registry.',
        affects='Only schools created after this changes - existing schools keep the connection they '
                "already have (control_plane's own set-db-url moves one school individually).",
        breaks_if_wrong='The next school created cannot be reached, or is created on the wrong server '
                        'entirely.'),
    Variable(
        name='BRIGHTSTARS_SECRET', category='Security', risk=RISK_CRITICAL, secret=True,
        effect=EFFECT_RESTART, default='random, new on every restart if left blank',
        purpose='The Flask session-signing secret, and (unless BRIGHTSTARS_DELIVERY_KEY is set '
                'separately) the key every saved school SMTP password, SMS API token and Paystack '
                'secret key is encrypted under.',
        when_to_change='Rarely, and on a deliberate schedule (see the delivery-key rotation wizard on '
                       'this page for the safe way to do it if BRIGHTSTARS_DELIVERY_KEY is not set '
                       'separately). In production it must be at least 32 characters or the '
                       'application refuses to start.',
        affects='Every signed-in session, everywhere. If BRIGHTSTARS_DELIVERY_KEY is not set, also '
               "every school's saved delivery and payment secrets.",
        breaks_if_wrong='Changing it signs out everyone, everywhere, at once. If BRIGHTSTARS_DELIVERY_KEY '
                        'is not set separately, it also makes every saved SMTP password, SMS API token '
                        "and Paystack key unreadable - use the rotation wizard, never edit this field "
                        'directly, unless no school has any of those saved yet.'),
    Variable(
        name='BRIGHTSTARS_ENV', category='Platform', risk=RISK_HIGH, effect=EFFECT_RESTART,
        default='development', aliases=('FLASK_ENV',),
        purpose='"production" or "development". Production marks session cookies Secure and enforces '
                "BRIGHTSTARS_SECRET's minimum length at start-up.",
        when_to_change='Once, when moving a deployment from development to a real, public launch.',
        affects='Cookie security and the start-up secret-length check.',
        breaks_if_wrong='Left on "development" in production, cookies are not marked Secure. Set to '
                        '"production" with a short BRIGHTSTARS_SECRET, the application refuses to '
                        'start at all.'),
    Variable(
        name='BRIGHTSTARS_PLATFORM_HOSTS', category='Platform', risk=RISK_HIGH,
        default='platform.localhost',
        purpose='Comma-separated hostnames (no ports) that serve the platform console, /marketing and '
                '/docs instead of a school. No school is ever served on these.',
        when_to_change="Pointing the console at the deployment's real platform address "
                       '(e.g. platform.yourdomain.com).',
        affects='Whether the console is reachable at all, and whether a school could accidentally be '
               'served on what should be the console address.',
        breaks_if_wrong='Set wrong, the console becomes unreachable (locking every admin out, including '
                        'the super admin, until this variable is fixed some other way) or a hostname '
                        'meant for a school starts serving the console instead.'),
    Variable(
        name='BRIGHTSTARS_PORTAL_DOMAIN', category='Platform', risk=RISK_HIGH, default='localhost',
        purpose="The domain every school's portal address is issued under: a school with code "
               '"alpha" is reachable at alpha.<this domain> the moment it is created.',
        when_to_change='Once, when moving from local development to the real domain schools will use.',
        affects="Every school's portal address, and any custom domain a school has pointed at it with "
               'a CNAME.',
        breaks_if_wrong="Every school's portal address changes at once; existing links and any school's "
                        'own CNAME (which points at the old address) stop working until updated.'),
    Variable(
        name='BRIGHTSTARS_TENANTS_DIR', category='Platform', risk=RISK_CRITICAL, default='tenants',
        purpose="Root folder holding every school's own files: question bank images, student, "
               'candidate and staff photographs, signatures, branding, receipt attachments, and its '
               'numbering rules.',
        when_to_change='Moving where school files are stored on disk, before any school has files, or '
                       'together with physically moving the folder.',
        affects='Every uploaded file, for every school.',
        breaks_if_wrong="Every school's existing images and files become unreadable (the folder they "
                        "were saved under is no longer where the application looks) until either the "
                        'files are moved to the new location or this is changed back.'),
    Variable(
        name='BRIGHTSTARS_DELIVERY_KEY', category='Security', risk=RISK_CRITICAL, secret=True,
        effect=EFFECT_RESTART, default='derived from BRIGHTSTARS_SECRET if left blank',
        purpose="The key every school's saved SMTP password, SMS API token and Paystack secret key "
               'is encrypted under at rest. Kept separate from BRIGHTSTARS_SECRET so the session '
               'secret can be rotated (an easy, cheap operation - it only signs everyone out) without '
               "touching schools' saved credentials at all.",
        when_to_change='On a deliberate rotation schedule, or if this key is ever suspected exposed - '
                       'always through the rotation wizard on this page, never by editing this field '
                       'directly.',
        affects="Every school's own saved mail server password, SMS API token and Paystack "
               'secret key.',
        breaks_if_wrong='Changed directly (not through the rotation wizard), every school with a saved '
                        'SMTP password, SMS API token or Paystack key can no longer read it back - '
                        'mail, SMS delivery and online payments silently stop working for that '
                        'school until it re-enters its credentials.'),
    Variable(
        name='BRIGHTSTARS_TRUSTED_PROXIES', category='Security', risk=RISK_HIGH, default='0',
        purpose='How many reverse proxies (nginx, Caddy, a load balancer) stand between the internet '
               "and this application. 0 means none: a visitor's address and scheme are taken from "
               'the connection itself and the X-Forwarded-* headers are ignored, since anyone can '
               'send them.',
        when_to_change='Deploying behind one or more reverse proxies, so the address recorded for a '
                       'visitor (audit log, sign-in rate limits) and the scheme used in email links '
                       '(https) are correct.',
        affects='What address is recorded in the audit log and counted against sign-in rate limits; '
               'whether links in emails use https.',
        breaks_if_wrong='Too high, a visitor can forge their own address (defeating rate limits and '
                        "the audit trail). Too low behind a real proxy, every visitor's address is "
                        "recorded as the proxy's own."),
    Variable(
        name='BRIGHTSTARS_DB_POOL_SIZE', category='Database', risk=RISK_MEDIUM, default='2',
        purpose="A school's own database connection pool size (per worker process). Small by "
               'default because most schools need very few connections at once.',
        when_to_change='A school with heavy concurrent traffic (e.g. exam day for a large school) is '
                       'seeing connection-pool exhaustion errors.',
        affects='How many simultaneous database connections one worker keeps open per school.',
        breaks_if_wrong='Too low under real load, requests wait for a free connection or time out. Too '
                        'high across many schools, the database server can run out of connections '
                        'itself.'),
    Variable(
        name='BRIGHTSTARS_DB_MAX_OVERFLOW', category='Database', risk=RISK_MEDIUM, default='3',
        purpose='How many extra connections a school\'s pool may open beyond BRIGHTSTARS_DB_POOL_SIZE '
               'under a burst of demand, before a request waits.',
        when_to_change='Together with BRIGHTSTARS_DB_POOL_SIZE, for the same reasons.',
        affects='How much a school\'s connection use may burst above its normal pool size.',
        breaks_if_wrong='Same failure modes as BRIGHTSTARS_DB_POOL_SIZE, just at a different threshold.'),
    Variable(
        name='BRIGHTSTARS_REGISTRY_POOL_SIZE', category='Database', risk=RISK_MEDIUM, default='10',
        purpose='The connection pool size for the platform registry itself (one engine, shared by '
               'every request regardless of school, so its default is larger than a single school\'s).',
        when_to_change='The whole deployment is large enough (many schools, many workers) that '
                       'registry lookups start queuing for a connection.',
        affects='Every request, of every school - the registry is consulted to find which school a '
               'hostname belongs to.',
        breaks_if_wrong='Too low under real load, every school\'s requests can start queuing on '
                        'registry lookups, not just one school\'s.'),
    Variable(
        name='BRIGHTSTARS_REGISTRY_MAX_OVERFLOW', category='Database', risk=RISK_MEDIUM, default='20',
        purpose='How many extra registry connections may open beyond BRIGHTSTARS_REGISTRY_POOL_SIZE '
               'under a burst of demand.',
        when_to_change='Together with BRIGHTSTARS_REGISTRY_POOL_SIZE, for the same reasons.',
        affects='How much registry connection use may burst above its normal pool size.',
        breaks_if_wrong='Same failure modes as BRIGHTSTARS_REGISTRY_POOL_SIZE, just at a different '
                        'threshold.'),
    Variable(
        name='BRIGHTSTARS_REGISTRY_CACHE_SECONDS', category='Platform', risk=RISK_LOW, default='10',
        purpose='How long a hostname -> school lookup is cached in each worker process. Bounds how '
               'long a suspension or a domain change takes to reach a worker that has already seen '
               'the school.',
        when_to_change='Wanting a suspension or domain change to take effect faster (lower it, at the '
                       'cost of more registry lookups) or reducing registry load on a very busy '
                       'deployment (raise it).',
        affects='How quickly every worker notices a school being suspended, reactivated, or given a '
               'new domain.',
        breaks_if_wrong='Set very high, a just-suspended school can keep serving traffic on a worker '
                        "that cached it as active for a while longer. Set to 0, every request's first "
                        'step is a fresh registry query.'),
    Variable(
        name='BRIGHTSTARS_PASSWORD_CHECK_CACHE_SECONDS', category='Platform', risk=RISK_MEDIUM, default='30',
        purpose='How long each worker remembers a signed-in person\'s password fingerprint before reading it '
               'from the database again. A password change ends the person\'s other sign-ins within this time.',
        when_to_change='Wanting another browser signed out sooner after a password change (lower it, or 0), '
                       'or wanting fewer database reads on a very busy deployment (raise it).',
        affects='Every signed-in request reads the password fingerprint from this cache.',
        breaks_if_wrong='Set very high, a password change takes that long to sign out other browsers. A stale '
                        'copy never ends a fresh sign-in: a disagreeing copy is re-read first.'),
    Variable(
        name='BRIGHTSTARS_ADMIN_CACHE_SECONDS', category='Platform', risk=RISK_MEDIUM, default='15',
        purpose='How long each worker keeps a school administrator\'s account, permissions and scopes after '
               'reading them.',
        when_to_change='Wanting a deactivation or a role change to reach other workers faster (lower it), or '
                       'fewer database reads per administrator request (raise it).',
        affects='How quickly a change to an administrator reaches workers other than the one that made it.',
        breaks_if_wrong='Set very high, a deactivated administrator can keep working on other workers for that '
                        'long. Set to 0, every administrator request reads the account from the database.'),
    Variable(
        name='BRIGHTSTARS_GZIP', category='Platform', risk=RISK_LOW, default='1',
        purpose='Whether pages, in-place fragments, JSON and the static text files are gzip-compressed for a browser '
                'that accepts it (core/speed.py). A static file is compressed once per version and kept.',
        when_to_change='Set to 0 where a proxy or CDN in front of the application already compresses responses.',
        affects='The size of every page and static file sent; not what any page shows.',
        breaks_if_wrong='Off with no proxy compressing: pages travel several times larger, slow on mobile data. On '
                        'behind a proxy that also compresses: harmless, the proxy sees it is already compressed.'),
    Variable(
        name='BRIGHTSTARS_BADGE_CACHE_SECONDS', category='Platform', risk=RISK_LOW, default='10',
        purpose='How long the counts on every admin page (unread notifications, open controls, unallocated '
               'payments) are kept in each worker.',
        when_to_change='Wanting the counts to update faster across workers (lower it), or fewer count queries '
                       'on very busy schools (raise it).',
        affects='The header and banner counts on every administrator page.',
        breaks_if_wrong='A count can lag a change made on another worker by this long. A change made on the '
                        'same worker always shows at once.'),
    Variable(
        name='BRIGHTSTARS_BRANDING_CACHE_SECONDS', category='Platform', risk=RISK_LOW, default='10',
        purpose='How long a school\'s computed branding (name, colours, logo, contact details) is kept in each '
               'worker.',
        when_to_change='Wanting a branding change to appear on other workers sooner (lower it), or fewer '
                       'database reads per page (raise it).',
        affects='Every page of a school\'s portal, its sign-in page and its documents.',
        breaks_if_wrong='Set very high, other workers keep showing the old name or colours for that long.'),
    Variable(
        name='BRIGHTSTARS_SCHOOL_ENGINE_IDLE_SECONDS', category='Database', risk=RISK_MEDIUM, default='600',
        purpose='How long a school\'s database connections may sit unused before they are closed. The next '
               'request for that school opens them again.',
        when_to_change='Many schools and worker processes exhausting the database\'s connection limit (lower it), '
                       'or schools that are used in bursts and suffer from reconnecting (raise it).',
        affects='The number of open connections to the database server from idle schools.',
        breaks_if_wrong='Set to 0, idle schools keep their connections open indefinitely, which can use up the '
                        'server\'s connection limit as schools are added.'),
    Variable(
        name='BRIGHTSTARS_MAX_UPLOAD_BYTES', category='Platform', risk=RISK_LOW, default='5242880',
        purpose='The largest single uploaded image (question images, photographs, logos) any school '
               'may save, in bytes. 5 MB by default.',
        when_to_change="A school's images are routinely being refused for being too large.",
        affects='Every image upload, for every school.',
        breaks_if_wrong='Set below BRIGHTSTARS_MAX_REQUEST_BYTES that is fine; set above it, an upload '
                        'this limit would otherwise allow is refused by the request-size limit first, '
                        'with a less helpful error.'),
    Variable(
        name='BRIGHTSTARS_MAX_REQUEST_BYTES', category='Platform', risk=RISK_LOW, default='8388608',
        effect=EFFECT_RESTART,
        purpose='The largest request body this application accepts at all - must stay at least as '
               'large as BRIGHTSTARS_MAX_UPLOAD_BYTES.',
        when_to_change='Together with BRIGHTSTARS_MAX_UPLOAD_BYTES.',
        affects='Every request with a body, for every school.',
        breaks_if_wrong='Set below the upload limit, every upload is refused outright, with a less '
                        'helpful error than the upload-size check would give.'),
    Variable(
        name='PORT', category='Platform', risk=RISK_LOW, default='5000', effect=EFFECT_RESTART,
        purpose='The port the built-in development server (python app.py) listens on.',
        when_to_change='Running more than one instance on the same machine, or avoiding a port '
                       'already in use.',
        affects='Only the built-in development server - a production deployment behind a real WSGI '
               'server and reverse proxy is usually configured there instead.',
        breaks_if_wrong='The server fails to start if the port is already in use.'),
    Variable(
        name='BRIGHTSTARS_SMTP_HOST', category='Delivery', risk=RISK_MEDIUM,
        purpose="The platform's shared mail server, used by any school that has not set up its own "
               'under "Email & SMS" in its own admin area. Left blank, email delivery through '
               'the shared account is disabled entirely (a school with its own account is unaffected).',
        when_to_change='Setting up (or moving) the platform\'s own shared mail account.',
        affects='Every school that has not configured its own SMTP account: password-reset emails, '
               'payment receipts, and any other platform mail for them.',
        breaks_if_wrong='Left blank or wrong, every school without its own mail account stops being '
                        'able to send email through the platform.'),
    Variable(
        name='BRIGHTSTARS_SMTP_PORT', category='Delivery', risk=RISK_MEDIUM, default='587',
        purpose="The shared mail server's port. Port 465 always uses implicit TLS automatically; any "
               'other port connects plain and upgrades with STARTTLS.',
        when_to_change='Together with BRIGHTSTARS_SMTP_HOST, matching what the mail provider expects.',
        affects='Every school using the shared mail account.',
        breaks_if_wrong='Mail delivery through the shared account fails or is attempted over an '
                        'unencrypted connection the provider refuses.'),
    Variable(
        name='BRIGHTSTARS_SMTP_USER', category='Delivery', risk=RISK_MEDIUM,
        purpose='The shared mail account username.',
        when_to_change='Together with BRIGHTSTARS_SMTP_HOST.',
        affects='Every school using the shared mail account.',
        breaks_if_wrong='Mail sent through the shared account is refused by the provider.'),
    Variable(
        name='BRIGHTSTARS_SMTP_PASSWORD', category='Delivery', risk=RISK_MEDIUM, secret=True,
        purpose='The shared mail account password.',
        when_to_change='The mail provider\'s password changed, or is being rotated.',
        affects='Every school using the shared mail account.',
        breaks_if_wrong='Mail sent through the shared account is refused by the provider.'),
    Variable(
        name='BRIGHTSTARS_SMTP_FROM', category='Delivery', risk=RISK_MEDIUM,
        default='same as BRIGHTSTARS_SMTP_USER if left blank',
        purpose='The sender address shown on mail sent through the shared account.',
        when_to_change='The shared account should show a different "from" address than its login '
                       'username.',
        affects='Every school using the shared mail account.',
        breaks_if_wrong='Mail may be sent from an address the provider does not allow, and be refused '
                        'or marked as spam.'),
    Variable(
        name='BRIGHTSTARS_SMTP_STARTTLS', category='Delivery', risk=RISK_MEDIUM, default='1',
        purpose='1 to use STARTTLS on the shared account\'s connection (almost always correct), 0 to '
               'disable it.',
        when_to_change='Almost never - only if the mail provider specifically does not support '
                       'STARTTLS on the configured port.',
        affects='Every school using the shared mail account.',
        breaks_if_wrong='Set to 0 against a provider that requires STARTTLS, the connection is refused '
                        'or sent unencrypted.'),
    Variable(
        name='BRIGHTSTARS_SMTP_SSL', category='Delivery', risk=RISK_MEDIUM,
        purpose='Overrides whether the shared account\'s connection uses implicit TLS. Leave blank; '
               'port 465 already gets this automatically.',
        when_to_change='The mail provider serves implicit TLS on a non-465 port.',
        affects='Every school using the shared mail account.',
        breaks_if_wrong='Set wrong, the connection is attempted with the wrong TLS mode and is '
                        'refused.'),
    Variable(
        name='BRIGHTSTARS_SMS_PAYER', category='Delivery', risk=RISK_MEDIUM, default='school',
        purpose='Who pays for the text messages parents receive (a report card released, a new fee, a '
               'payment received, the first message of a conversation the school starts). "school": every '
               'school connects its own BulkSMS Nigeria account and pays for its own messages. "platform": '
               "the platform's one account (the token and sender below) texts for every school and the "
               'platform pays; schools are not asked for SMS details. "either": a school\'s own account '
               'when it has one, otherwise the platform\'s.',
        when_to_change='Deciding whether the platform absorbs (or bills for) its schools\' SMS cost.',
        affects='Every school: whether they must connect their own SMS account and who is charged.',
        breaks_if_wrong='Set to "platform" with no token below, no school can send SMS. Set to "school", '
                        'schools that relied on the shared account stop sending until they add their own.'),
    Variable(
        name='BRIGHTSTARS_SMS_API_TOKEN', category='Delivery', risk=RISK_MEDIUM, secret=True,
        purpose="The platform's BulkSMS Nigeria API token (https://www.bulksmsnigeria.com/user/api-tokens), "
               'used when BRIGHTSTARS_SMS_PAYER is "platform", or "either" for a school without its own.',
        when_to_change='Setting up the platform account, or rotating the token.',
        affects='Every school that texts through the platform account.',
        breaks_if_wrong='Texts through the platform account are refused until the token is right.'),
    Variable(
        name='BRIGHTSTARS_SMS_SENDER_ID', category='Delivery', risk=RISK_MEDIUM,
        purpose='The sender name parents see on a text sent through the platform account: 3 to 11 letters and '
               'digits, registered and approved on the BulkSMS Nigeria account.',
        when_to_change='Together with BRIGHTSTARS_SMS_API_TOKEN.',
        affects='Every school that texts through the platform account.',
        breaks_if_wrong='BulkSMS Nigeria refuses texts from a sender name that is not approved.'),
    Variable(
        name='BRIGHTSTARS_SMS_GATEWAY', category='Delivery', risk=RISK_LOW, default='direct-refund',
        purpose='The BulkSMS Nigeria route for texts sent through the platform account: direct-refund '
               '(cheapest, refunded if not delivered), direct-corporate (most reliable) or dual-backup.',
        when_to_change='Texts are not reaching parents on numbers that block promotional routes.',
        affects='Every school that texts through the platform account.',
        breaks_if_wrong='An unknown value falls back to direct-refund.'),
    Variable(
        name='BRIGHTSTARS_ALERT_WEBHOOK', category='Ops', risk=RISK_LOW, secret=True,
        purpose='A URL (e.g. a Slack "Incoming Webhook") that receives a JSON POST for the handful of '
               'log lines that mean something is degraded enough to page on: the registry being '
               'unreachable, a job exhausting its retries, a burst of delivery failures, or a sign-in '
               'rate limit being hit repeatedly. Left blank, these are only logged, exactly as before '
               'this existed.',
        when_to_change='Setting up (or moving) where operational alerts are sent.',
        affects='Nothing about the application\'s own behaviour - only where its degradation signals '
               'are also sent, on top of the log.',
        breaks_if_wrong='Set to an unreachable or wrong URL, alerts silently fail to deliver (this is '
                        'deliberately fire-and-forget) - the log lines are still written either way.'),
    Variable(
        name='BRIGHTSTARS_CHROME', category='Ops', risk=RISK_LOW,
        default='auto-detected',
        purpose='The Chrome or Chromium program used to draw a candidate\'s result image. Found '
               'automatically on Windows and under the usual names on Linux and macOS; set this only '
               'if it lives somewhere else.',
        when_to_change='Result images stop rendering because Chrome/Chromium cannot be found '
                       'automatically on this machine.',
        affects='Only candidate result-image rendering.',
        breaks_if_wrong='Result images fail to render until this points at a real Chrome/Chromium '
                        'executable.'),
    Variable(
        name='BRIGHTSTARS_PG_DUMP', category='Ops', risk=RISK_LOW,
        default='found automatically',
        purpose='The pg_dump program used to export a school\'s database (python -m control_plane '
               'export-tenant). Found automatically: pg_dump on the PATH, then the newest PostgreSQL '
               'install under Program Files on Windows or /usr/lib/postgresql on Linux. Set this only '
               'if pg_dump lives somewhere else.',
        when_to_change='School exports fail with "pg_dump was not found" on this machine.',
        affects='Only school exports (database.sql in an export).',
        breaks_if_wrong='Exports fail until this points at a pg_dump of the same or a newer major '
                        'version than the PostgreSQL server.'),
    Variable(
        name='BRIGHTSTARS_CONTACT_EMAIL', category='Platform', risk=RISK_LOW,
        purpose='Where the public /marketing page\'s contact buttons point.',
        when_to_change='The platform\'s own contact address changes.',
        affects='Only the public marketing page.',
        breaks_if_wrong='The marketing page\'s contact buttons point at the wrong (or no) address - '
                        'cosmetic, nothing else is affected.'),
]

BY_NAME = {v.name: v for v in VARIABLES}
CATEGORIES = sorted({v.category for v in VARIABLES})


def get(name):
    return BY_NAME.get(name)


def grouped():
    """Variables grouped by category, each group in the order declared above."""
    out = {c: [] for c in CATEGORIES}
    for v in VARIABLES:
        out[v.category].append(v)
    return out
