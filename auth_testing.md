# Auth Testing Playbook (Emergent Google Auth)

Auth-Gated App Testing Playbook

## Step 1: Create Test User & Session (mongosh)
```
use('test_database');
var userId = 'test-user-' + Date.now();
var sessionToken = 'test_session_' + Date.now();
db.users.insertOne({ user_id: userId, email: 'test.user.'+Date.now()+'@example.com', name:'Test User', picture:'https://via.placeholder.com/150', created_at:new Date() });
db.user_sessions.insertOne({ user_id: userId, session_token: sessionToken, expires_at:new Date(Date.now()+7*24*60*60*1000), created_at:new Date() });
print('Session token: '+sessionToken); print('User ID: '+userId);
```

## Step 2: Backend API
- GET /api/auth/me  with header `Authorization: Bearer <session_token>`  → returns user
- POST /api/auth/session with header `X-Session-ID: <id>` → exchanges + sets cookie
- POST /api/auth/logout → clears session/cookie

## Step 3: Browser Testing (Playwright)
Set cookie `session_token` (httpOnly, secure, sameSite=None, path=/) on the app domain, then goto /hub/index.html — should render the shell, not redirect to /login.

## Notes
- redirect_url = window.location.origin + '/hub/index.html' (main app, NOT login page). Never hardcode.
- Session exchange calls https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data (backend only).
- Cookie: httpOnly, path="/", secure=True, samesite="none". Expiry 7 days, timezone-aware.
- Auth helper: session_token from cookie first, then Authorization header.
- Public surface /diagnose stays open (lead magnet). Gated: /hub, /settings.

## Checklist
- users doc has user_id (UUID); sessions user_id matches; queries use {"_id":0}
- /api/auth/me returns user (not 401) with valid token
- hub loads without redirect when authed; redirects to /login when not
