# Phase 6I — Strict Workspace Boundary

## Locked navigation rule
Every authenticated administrator, including Super Admin, must explicitly choose a workspace immediately after sign-in and before the administration sidebar is displayed.

Available workspaces:
- Entrance Examination — operates on entrance-examination candidates and related examination resources.
- School Portal — operates on enrolled school students and related school-management resources.

Workspace Home is a neutral boundary, not an operational workspace. It shows only the two workspace choices. Returning to Workspace Home clears the active workspace selection.

## Switching rule
An administrator cannot switch directly from one workspace to the other from the active workspace sidebar. The administrator must first exit the current workspace to Workspace Home, then deliberately select the other workspace.

The server enforces this boundary as well as the UI. Direct URL navigation to a route belonging to the other workspace returns the administrator to Workspace Home without silently changing the active workspace.

## Terminology
- Entrance Examination: candidates
- School Portal: students

## Future student portal
The current candidate login portal remains the active entrance-examination participant portal. A separate student login portal will be built for School Portal students.
