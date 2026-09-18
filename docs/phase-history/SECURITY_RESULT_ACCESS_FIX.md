# Crainbow CBT — Candidate Submission / Result / Access-Control Fix

This build preserves the existing CBT application and changes only the candidate-facing submission/result and retake-access behaviour.

## Candidate behaviour

After submission, the candidate sees only:

> **Examination Submitted Successfully**
>
> Your examination has been submitted successfully.
> Your responses have been securely recorded.
>
> **Your result will be released by the school.**

The candidate is not shown:

- score
- percentage
- grade
- correct answers
- wrong answers
- answer review
- performance analysis
- a "Start Another Test" result link

The candidate `/result` route does not pass score data to the candidate template and sends `Cache-Control: no-store` headers.

## Repeat-examination protection

A candidate who has submitted an examination cannot log in and take that same examination again. An examination that is still active can be resumed rather than duplicated.

A completed examination can only be taken again after an administrator uses the **Grant Retake** action. The permission is one-time and is consumed when used.

## Admin behaviour

Administrators retain access to:

- candidate score and percentage
- candidate answers
- correct answers
- detailed candidate script/review
- printable candidate result/script
- regrade
- one-time retake permission

## Verification

Dependency-free static verification:

```text
python security/verify_candidate_result_static.py
```

End-to-end verification (requires the project's Flask dependency):

```text
python security/test_candidate_result_access.py
```

The end-to-end test uses a temporary SQLite database and does not modify the production `cbt.db`.
