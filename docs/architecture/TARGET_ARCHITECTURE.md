# Crainbow — Target Production Architecture

## Principle
One integrated Crainbow platform, logically modular, API-first, security-first, with one authoritative identity architecture and one authoritative production database.

## Layers
1. Presentation: responsive web client.
2. API/application boundary: versioned secure API/services.
3. Domain modules: identity/auth, school, learning, assessment, examination, results, analytics, administration, audit.
4. Persistence: PostgreSQL in production, migration-managed schema.
5. Infrastructure: HTTPS/reverse proxy, secrets, monitoring, backups, recovery, staging and production environments.

## Examination boundary
The Examination Engine is a protected domain within the same platform:
Question Bank → Version → Blueprint → Published Paper Version → Attempt → Question Snapshot → Responses → Server-side Scoring → Immutable Result → Ranking.

The student/candidate device must never receive the authoritative answer key or unrestricted question-bank content.

## Assessment parity requirement
Student Portal Tests and Examinations must use the same core interaction model as the Entrance Examination:
- student/candidate explicitly starts the assessment;
- timer begins only at that start event;
- one question is displayed at a time;
- answer selection is persisted through Save & Next;
- previous/next review behavior is supported where configured;
- submission is explicit or server-enforced on expiry;
- server is authoritative for timing and submission;
- answers/results survive normal refresh/reconnect scenarios as designed.

## Content and database
Question banks become controlled content records with status/version/provenance. JSON remains an import/export interchange format, not the runtime authority for published examinations. PostgreSQL is the production target with versioned migrations, constraints, indexes and transactional integrity.

## Clients
The web application is the current primary client. Future native Android/iOS clients consume the same secure API; they do not contain a second authoritative database.

## Environment model
Development → Staging → Production, each with isolated configuration/secrets and appropriate databases. The current LAN server address is for development/school testing only.

## UX continuity
The platform-wide type-ahead/suggestion behavior already valued by the project owner is a retained product requirement. It should be preserved and standardized across relevant searchable/selectable inputs rather than removed during refactoring.
