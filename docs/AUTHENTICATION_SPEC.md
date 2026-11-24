# Authentication contract

**Status:** trusted handler registry and runtime flows are **implemented**.
Physical FiberHome equivalence is **not evaluated**. A generic/manual handler is
**planned**. The Profile Builder can list trusted handler identifiers and select
one declaratively; its offline `test` command is fixture-only and does not
perform real authentication.

Authoritative files: `auth_handlers.py`, `connector_executor.py`,
`config/connectors.json`, `config/default_credentials.json`, `gateway.py`, and
the FiberHome executor tests.

## Trust model

Profiles choose a handler by name. They never supply Python, shell, templates,
or evaluated expressions. All auth behavior lives in fixed Python classes in
`auth_handlers.py`. The registry is closed:

`none`, `http_basic`, `http_digest`, `form`, `session`, `token`,
`fiberhome_aes`

`auth_handler()` rejects any other identifier. Profile fields such as
`fields`, `success`, and `session` are data consumed by those handlers.

Authentication happens only after `POST /api/connect`. Discovery and
`/api/login-defaults` are unauthenticated. There is no auto-login after
identification.

## Flow selection

`ConnectorExecutor._authenticate()`:

1. Sorts flows by `order`.
2. Resets auth state, derived values, endpoint results, HTTP auth, and
   `Authorization` headers before each flow.
3. Runs the handler registered for the flow type.
4. Treats `AuthenticationError` and `EndpointError` as a failed flow and tries
   the next one.
5. Marks the executor authenticated only after a success check passes.
6. Fails closed if no flow succeeds.

`connect()` then returns only `ok`, `profile_id`, `display_name`, and `gateway`.
The profile ID is internal to the backend/API response and is not shown in the
dashboard.

## Handler behavior

| Identifier | Behavior |
|---|---|
| `none` | Executes the optional login endpoint, then applies the success check. |
| `http_basic` | Sets `requests.HTTPBasicAuth(username, password)` on the session and applies the success check. |
| `http_digest` | Sets `requests.HTTPDigestAuth(username, password)` on the session and applies the success check. |
| `form` | Executes the declared login endpoint, checks success, and applies declared session state. |
| `session` | Same form flow; used by profile-based session login. |
| `token` | Form flow plus an `Authorization: Bearer <token>` header from the session `id_field`. |
| `fiberhome_aes` | Fetch seed, encrypt password through `gateway.fhencrypt`, submit login, check success, apply session state. |

The success check requires a declared response field. It supports `allowed`
values and a `not_equal` rejection value. A missing field fails the flow.

Session metadata may declare an `id_field`, `store`, `validity_field`, and
`invalid_values`. Stored state contains session identifiers, never passwords.
During collection, a declared invalid value in any endpoint response triggers
one forced reauthentication. A second expiry raises `SessionExpired`; the Flask
route clears session state and returns HTTP 401.

## FiberHome AES flow

The shipped profile uses the `fiberhome_aes` handler through endpoint IDs
`seed` and `login`:

1. `GET /cgi-bin/ajax` with `ajaxmethod=get_acs_random`. Endpoint is
   `auth: false`.
2. Read `acsRandom` and `sessionid`. Missing either value fails the flow.
3. Delegate encryption to `gateway.fhencrypt(password, acsRandom)`. The handler
   does not reimplement cryptography.
4. Store the result in `executor.derived["encrypted_password"]` and the session
   ID in `executor.state["sessionid"]`.
5. `POST /cgi-bin/ajax` with `ajaxmethod=do_login`, `username` from user input,
   `loginpd` from `derived.encrypted_password`, literal `port: 0`, and
   `sessionid` from state. Endpoint is `auth: false`.
6. Require `login_result` in `{0, 1}`.
7. Store `sessionid` from the login response as session state; monitor
   `session_valid` for invalid value `0`.

`fhencrypt()` implements AES-128-CBC with key and IV both derived from
`seed[6:][:16]` (falling back to the documented literal when empty), padded or
truncated to 16 bytes. Password bytes use PKCS#7-style padding. Output is
uppercase hexadecimal.

Deterministic vector from the current implementation (fake credentials only):

```text
password = "contract-password"
seed     = "abcdefghijklmnopqrstuv"
cipher   = "EE46080EA2CD73D22949926CE91A76D0C573FD0F32515DCADE46133111EA03FA"
```

The plaintext password must not appear in the login request, derived values,
logs, success messages, profiles, or public API payloads.

## Logout

The FiberHome profile declares `logout_endpoint: "logout"` with dependency
`login_user`:

1. Fetch `get_login_user`.
2. Map `login_user` values to the username: `4 → superadmin`, `2 → admin`,
   otherwise `common`.
3. `POST` `do_logout` with the derived username and stored `sessionid`.
4. Reset executor auth state in a `finally` block.

A logout failure is reported by `/api/disconnect` as
`router_logout: false`; local disconnect still succeeds.

## Credential suggestions

`config/default_credentials.json` contains editable pre-fill values and their
provenance. Each suggestion provides one default username and password; no
fallback password is configured. `app.py` uses the submitted credentials
exactly once, without substitution or retry, and the frontend leaves
user-edited credential fields untouched.

No credential values are copied into `config/connectors.json`. The login form
returns only gateway, supported flag, username, password, and message; no
connector or evidence details.

## Limitations

- **Not evaluated:** mocked tests and observed endpoint names do not prove
  physical ONT authentication equivalence. The profile remains `observed`.
- **Partial:** no generic handler for arbitrary manual/proprietary flows beyond
  the fixed registry and HAR classification metadata.
- **Implemented:** `auth-handlers --json` lists the closed identifier set and
  `configure --auth-handler` selects one without profile code. Offline `test`
  uses fixture-backed discovery/collection and never performs real login.
- The legacy `RouterClient` auth modes in `gateway.py` are comparison code only
  and are not used by `app.py`.
