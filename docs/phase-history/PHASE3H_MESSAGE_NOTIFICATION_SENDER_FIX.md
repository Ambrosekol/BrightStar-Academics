# Phase 3H — Message Notification Sender Fix

## Issue
Unread direct-message badges showed only a count (for example, `Messages 1`) and did not visibly identify the administrator who sent the unread message.

## Fix
- The Messages item in the administrator navigation now shows the sender when unread messages exist: `New from <Staff Name>` for one sender.
- When multiple unread senders exist, the navigation shows the first two sender names and an additional-count indicator.
- The existing page-level unread notification continues to identify the sender and timestamp.
- The navigation tooltip remains available as an accessible/hover detail.
- CSS cache-busting was incremented so browsers load the updated navigation styling immediately.

## Regression verification
- Current contract suite: 7 passed, 0 failed.
- Message notification sender regression: PASS.
- Python AST parse: PASS.
- Packaged Super Admin boundary count: 0.


## Phase 3H Live Messaging Enhancement

### User-observed issue
Unread administrator messages were not visible until the browser page was refreshed.

### Fix
- Added a lightweight authenticated `/admin/administration/messages/unread-state` endpoint.
- Added a 3-second background polling loop to the existing administrator UI.
- New unread messages update the Messages badge and sender name without a manual refresh.
- A small in-app notification toast identifies the sender and provides an **Open message** action.
- When an administrator is already viewing a conversation, newly arrived messages are pulled into the thread automatically.
- Polling uses `cache: no-store` and server-side no-cache headers so stale LAN/browser responses are not reused.
- No WebSocket server, broker, database replacement, or change to the existing messaging data model is required.

### Local-network behaviour
This is near-real-time delivery with a maximum normal detection interval of approximately 3 seconds. It works with the existing Flask/local-network deployment, including when multiple computers connect to the same server IP.

A true push model (WebSocket or Server-Sent Events) could reduce the delay further, but would add a persistent connection layer and deployment considerations. For this school LAN and the existing architecture, the 3-second polling implementation is the safer professional choice.

### Verification
- Python syntax/AST: PASS
- JavaScript syntax: PASS
- Existing contract suite: 7 passed, 0 failed
