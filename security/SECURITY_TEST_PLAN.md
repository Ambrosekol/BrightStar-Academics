# Crainbow CBT — Phase 6F
## Candidate Management & Secure Examination Access

Phase 6F adds a deployment-readiness security and testing layer around the existing CBT engine.

### Candidate access controls
- Unique candidate login identity.
- Examination eligibility checks.
- One active attempt per candidate/examination.
- Configurable attempt policy.
- Session ownership checks.
- Server-side examination timing.
- Candidate result hidden during the examination.
- Inactive examinations cannot be started.

### Session/security controls
- Session timeout handling.
- Server-side validation of candidate/session ownership.
- Protection against submitting another candidate's attempt ID.
- Protection against changing answers after an attempt is closed.
- Closed/expired attempts cannot be resumed as active attempts.
- Administrative actions are separated from candidate actions.
- Audit events are recorded for important examination events.

### Testing checklist
1. Valid candidate login.
2. Invalid candidate login.
3. Candidate attempts another candidate's session.
4. Candidate opens an inactive examination.
5. Candidate submits twice.
6. Candidate answers after expiry.
7. Candidate changes an answer after submission.
8. Candidate attempts to access another candidate's result.
9. Candidate attempts to access the admin area.
10. Administrator views candidate-only endpoints.
11. Duplicate candidate registration.
12. Duplicate active examination attempt.
13. Browser refresh during an active examination.
14. Network interruption and reconnect.
15. Timer reaches zero while the browser is idle.
16. Candidate changes system clock.
17. Candidate manipulates a submitted score in browser developer tools.
18. Candidate sends an invalid question/option ID.
19. Candidate submits an answer belonging to another question.
20. Admin deactivates an examination while candidates are taking it.

### Deployment rule

A test is considered passed only when the server rejects the invalid action and the valid workflow remains usable.

Do not deploy the real entrance examination until the critical tests have passed on the actual school server and at least two client computers.
