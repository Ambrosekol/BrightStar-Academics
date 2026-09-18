# Phase 6F Deployment Checklist

## Before Tuesday
- [ ] Back up the database.
- [ ] Confirm the server computer has a fixed/local LAN IP.
- [ ] Confirm all client computers can reach the server.
- [ ] Test candidate login from at least two client computers.
- [ ] Test one-candidate/one-attempt restriction.
- [ ] Test timer expiry.
- [ ] Test manual submission.
- [ ] Test answer changes.
- [ ] Test inactive examination blocking.
- [ ] Test that candidates cannot see scores before submission.
- [ ] Test admin login separately.
- [ ] Test result visibility after submission.
- [ ] Test server restart procedure.
- [ ] Test database backup/restore.
- [ ] Disable or change all default passwords before real use.
- [ ] Load only the final verified question banks.
- [ ] Conduct a full mock examination before admitting candidates.

## During the examination
- Keep the server computer powered and connected to the LAN.
- Do not close the server application.
- Do not change the server computer's clock.
- Keep a printed emergency attendance/candidate list.
- Keep a database backup before the first candidate starts.
- If a client computer fails, do not create a second attempt unless the admin policy explicitly permits it.

## After the examination
- Verify all attempts are submitted/expired.
- Export results.
- Back up the database.
- Preserve the audit log.
