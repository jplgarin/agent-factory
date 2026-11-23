# ADR 0007: Private network for the gateway, and Google OAuth without a public endpoint

Status: accepted (2026-11-23)

## Context

Decision 6 in PLAN section 6: the gateway uses per-space bearer tokens behind Caddy TLS **and** is reachable only over Tailscale. Nothing is public in this phase. Google's OAuth authorization code flow ends with a browser redirect to a redirect URI, so the design must give the browser somewhere to land without exposing the gateway.

Verified on 2026-11-23:
- Google's OAuth guide for installed (desktop) apps supports a loopback redirect `http://127.0.0.1:<port>` (or `http://[::1]:<port>`) on a random free port, with PKCE (`code_verifier`, `code_challenge`), and says an installed app cannot keep its `client_secret` confidential.
- Tailscale's guide "Caddy certificates on Tailscale": Caddy obtains the HTTPS certificate for a `*.ts.net` name from the machine's local Tailscale daemon.
- Tailscale's HTTPS guide: certificates are recorded in the public Certificate Transparency ledger, so machine names and the tailnet DNS name become public (the service stays unreachable; the name does not stay secret).
- Docker's packet filtering guide: Docker routes published-port traffic in the `nat` table, before the `INPUT` chain that `ufw` uses, so `ufw` rules do not protect published container ports.

## Decision

### Network

1. The VPS joins the tailnet. Caddy serves the gateway at `https://<machine>.<tailnet>.ts.net` with the certificate from the local Tailscale daemon. The machine name is generic and carries no personal or company information, because it will appear in the public CT ledger.
2. No service listens on the public interface. Docker Compose publishes ports only on `127.0.0.1` (Caddy is the only listener on the Tailscale address). Firewall rules are a second layer, never the only one, because of the Docker and `ufw` interaction above.
3. Temporal UI and admin endpoints are reachable only over the tailnet, like the gateway. SSH is reached over the tailnet; the VPS provider's console is the break-glass path (documented in the runbook).
4. Tailscale ACLs allow only your own devices to reach the VPS on 443 (and SSH). Tailnet membership is not authorization: every gateway request still needs the bearer token for the relevant space (G10).

### Google OAuth (loopback flow from your machine)

One Google Cloud project per space (G9), each with an OAuth client of type **Desktop app**. Re-authorising a mailbox is a CLI command on your machine while connected to the tailnet: `af mailbox auth <mailbox_id>`.

1. **Start.** The CLI calls `POST /oauth/google/start {mailbox_id}` with the space's bearer token. The gateway starts a `MailboxAuthWorkflow` in that space's Temporal namespace. An activity in the space's worker generates `state` and the PKCE `code_verifier`, stores the verifier encrypted with the space key (ADR 0005) in a space-scoped pending row that expires after 10 minutes, and returns `client_id`, `state` and `code_challenge`. The verifier never leaves the space's worker.
2. **Consent.** The CLI listens on `127.0.0.1:<random port>` and opens your browser at Google's authorization URL with scope `https://www.googleapis.com/auth/gmail.modify`, `access_type=offline`, `prompt=consent`, the challenge, the state and the loopback redirect URI.
3. **Callback.** Google redirects your browser to the loopback listener. The CLI checks `state` and sends `POST /oauth/google/complete {state, code, redirect_uri}` over the tailnet.
4. **Exchange inside the space's worker.** The gateway passes the code to the waiting workflow. A worker activity exchanges `code` plus the decrypted `code_verifier` at Google's token endpoint, then refuses and revokes the grant unless:
   - the granted scopes are exactly `gmail.modify`, and
   - the authorised account's address (Gmail `users.getProfile`) equals the address configured for `mailbox_id` in `AF_CONFIG_DIR`.
   On success it encrypts the refresh token (ADR 0005), stores it, deletes the pending row, and writes an audit event. The CLI reports success or the specific reason for refusal (wrong account for this mailbox id, wrong scopes, expired flow).
5. **What each party sees.** Your machine sees only the single-use authorization code and the challenge. The gateway sees the code in transit. Only the space's worker sees the verifier and the tokens. Temporal history holds the state and the code, which are useless without the verifier and expire within minutes.

## Alternatives considered

- *Public HTTPS callback on the VPS*: simplest, and what most guides assume. Rejected by decision 6 (nothing public in this phase).
- *Web-application OAuth client redirecting to the `ts.net` name*: the redirect is performed by your browser, so Google does not need to reach the VPS. It ties the OAuth client to a hostname published in CT logs and to Google's rules for web redirect domains (not checked). The loopback flow is the one Google documents for this situation.
- *CLI performs the token exchange itself*: the refresh token would pass through your machine. Kept only as the fallback below.
- *Device authorization flow*: Google limits it to a list of allowed scopes; whether `gmail.modify` is on that list is **unverified**, and it is not needed given the loopback flow.

## Consequences

- Re-authorising a mailbox needs your machine, the CLI and a tailnet connection. While the consumer app is in Testing (decision 5), this happens weekly until the G9 report leads to a different choice.
- The Desktop client secret is stored in the secret manager like any other secret, even though Google states it cannot be confidential for installed apps.
- Future public webhooks (WhatsApp) are out of scope here and get their own ADR.

## Unverified, checked in M5a

- That Google accepts the code exchange from a different host (the VPS) than the one running the loopback listener, with the same `redirect_uri` and the verifier. It is a server-to-server POST and nothing in the guide ties it to the listener's host, but it has not been tried. Fallback if refused: the CLI performs the exchange and sends the refresh token over the tailnet straight into the `MailboxAuthWorkflow` (the gateway forwards it without storing it), and the token is held only in memory on your machine.

## How it will be verified

- M0: a test reads `docker compose config` and fails if any published port binds an address other than `127.0.0.1`.
- M4: gateway tests for missing, wrong-space and wrong-token requests.
- M5a: tests against a fake Google token endpoint for state mismatch, expired flow, wrong account, extra or missing scopes; each stores nothing and revokes the grant.
- First deploy: a port scan of the VPS public address from outside the tailnet finds no open TCP ports, and the result goes in the deploy PR.
