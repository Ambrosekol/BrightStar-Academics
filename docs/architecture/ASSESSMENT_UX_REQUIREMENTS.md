# Crainbow — Student Assessment UX Contract

This is a locked acceptance requirement for the final web build.

## Scope
Applies to Student Portal Tests and Examinations. Practice may reuse the same engine where appropriate. Entrance Examination remains the reference interaction model.

## Required interaction
1. Student opens an available assessment information/start screen.
2. **Timer does not start merely because the student opens the assessment page.**
3. Student presses **Start Test/Start Examination**.
4. Server creates/activates the attempt and records the authoritative `started_at`/`expires_at`.
5. The UI presents **one question at a time**.
6. Student selects an option.
7. Student presses **Save & Next**; the response is persisted server-side.
8. Student can review previously answered questions where the assessment policy allows it.
9. Student presses **Submit** when finished.
10. Server validates the attempt state and deadline before accepting submission.
11. If the deadline is reached first, the server automatically finalizes/submits the attempt according to the assessment policy.
12. Results are generated server-side.

## Security requirements
- Never trust the browser timer as the authoritative timer.
- Never send correct answers to the student client.
- A student cannot submit to another student's attempt.
- A completed/released attempt cannot be silently restarted.
- Refresh/reconnect must not reset the timer or lose already acknowledged responses.

## Regression requirements
The final test suite must verify this flow for at least:
- Student Test
- Student Examination
- Entrance Examination
