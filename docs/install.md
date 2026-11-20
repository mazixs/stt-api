# Install Kamerton

[Русская версия](install.ru.md)

| What you want to do | Where to start | Ready when |
|---|---|---|
| Try Kamerton on this computer, no DNS | [Local](#local-no-dns) | `http://localhost:8091` opens |
| Already have Nginx Proxy Manager (NPM) or another HTTPS proxy | [Existing HTTPS proxy](#existing-https-proxy) | The owner sign-in opens at your HTTPS address; no Caddy |
| No HTTPS proxy, and TCP ports 80 and 443 are free | [Caddy](#caddy) | Caddy serves your HTTPS address and the owner can sign in |

You need Git, Python 3.10+ and a local Docker Engine for Linux containers with Compose 2.24.4+. Use Docker Engine 28+: [earlier versions could expose loopback-published ports on the local network](https://docs.docker.com/engine/network/port-publishing/).

**Windows with Docker Desktop:** use an Ubuntu terminal in WSL 2, not PowerShell. In Docker Desktop, enable the WSL 2 backend and **Settings → Resources → WSL Integration** for Ubuntu. Run `cd ~` in Ubuntu before cloning. Keep the checkout and any `--root` directory on the Linux filesystem, not `/mnt/c`, so the owner database and private files use Linux permissions.

In Bash, clone a revision containing `scripts/kamerton.py`: `git clone https://github.com/mazixs/stt-api.git && cd stt-api`.

**Already have a working installation?** Go to [migration](#migrate-an-existing-stack), not `init` or `setup`. By default, the CLI writes into the checkout. For a separate empty installation directory, pass `--root` before the command on every invocation. Do not run direct `docker compose up` against the CLI installation's directories: it starts a separate project.

## Local, no DNS

```bash
python3 scripts/kamerton.py setup local
```

Open `http://localhost:8091` on the computer where the browser and Docker run. Keep the loopback bind; this mode has no owner login and isn't for publishing on your network. If you want a different `HOST_PORT` or a local `API_KEY`, run `python3 scripts/kamerton.py init local` **instead of** `setup local`, edit `.env`, then run `python3 scripts/kamerton.py start`. The same key allows both transcription and console administration. Go to [first transcription](#first-transcription).

## Existing HTTPS proxy

**Browser or client → HTTPS (your proxy) → HTTP (Kamerton on port 8812).** Allow the published port only from the proxy; don't forward it from the internet. Binding an interface is not an IP allowlist: enforce this in the network firewall or in host rules that apply to Docker-published traffic. Do not start Caddy. Choose a name and certificate under [DNS and certificates](#dns-and-certificates).

1. Run `python3 scripts/kamerton.py init npm`. Edit `.env.npm` using [the example](../.env.npm.example):
   - `KAMERTON_BIND`: exact IPv4 address of a Kamerton host interface reachable from the proxy, not `0.0.0.0`. `KAMERTON_PORT` is `8812` by default.
   - `PUBLIC_ORIGIN`: browser origin, e.g. `https://stt.example.org`. Replace the example name; don't add a path, trailing slash or `:443`.
   - `TRUSTED_PROXY_IP`: the **one IP address Kamerton sees as the immediate connection peer**, not a CIDR, client IP or `X-Forwarded-For` value. With containers and NAT the peer may differ from the proxy's advertised IP. Check the actual network path and use a stable, proxy-only route; don't trust a shared gateway if other traffic can reach the backend presenting the same peer.
2. Run `python3 scripts/kamerton.py owner create` and follow the prompts. Then run `python3 scripts/kamerton.py start`. The first owner is created from the host, not on the website; keep the credentials in your password manager.
3. In NPM, add a **Proxy Host**. **Domain Names** is the public-facing name from `PUBLIC_ORIGIN`; **Scheme** is `http`; **Forward Hostname / IP** is the backend address reachable *from NPM* (normally `KAMERTON_BIND`), not the public name; **Forward Port** is `8812` (or the value you set). In **SSL**, select or request the certificate for the front name and enable **Force SSL**. Don't use NPM's `localhost` for a backend in another container or on another machine.
4. For official Nginx Proxy Manager, paste [the complete `location /` block](../deploy/npm-location.conf) into the proxy host's **Advanced** tab, not **Custom Locations**. It replaces NPM's default location; if you use an NPM Access List, don't assume those rules are preserved. For NPMplus or another proxy, verify the effective forwarding rule instead of pasting the NPM block: exactly one `Host` preserving the front name and port, `X-Forwarded-Proto: https` for TLS, and `X-Kamerton-Client-IP` **overwritten** with the client IP observed by the proxy (or removed if it isn't known). Never pass through a client-supplied value. Preserve `Origin`, cookies and `Authorization`. Allow multipart overhead above `MAX_UPLOAD_MB` and disable response buffering for live events.

**Ready when:** the HTTPS site opens at `/login`, you can sign in, and `GET /v1/models` without a key returns `401` through HTTPS. A direct request to the backend from another machine should be blocked or refused. Go to [first transcription](#first-transcription).

## Caddy

**Browser or client → HTTPS (Caddy) → HTTP (Kamerton on an internal Docker network).** Only Caddy binds host ports 80 and 443. Don't use this route alongside a proxy already listening on those ports.

1. Run `python3 scripts/kamerton.py init caddy`. In `.env.public`, set `KAMERTON_DOMAIN` to your DNS name without a scheme, port or path. [The example](../.env.public.example) also shows `CADDY_BIND` and the private Docker network settings; change the subnet and both addresses together if they conflict with an existing network.
2. Point DNS at Caddy and arrange certificate issuance as described below. Run `python3 scripts/kamerton.py owner create`, then `python3 scripts/kamerton.py start`.
3. Open your HTTPS name and sign in using the owner credentials. For Caddy-specific operations, see the [manual Caddy guide (Russian)](public-access.md).

**Ready when:** the browser trusts the certificate, the owner signs in and no Kamerton HTTP port is published on the host. Then try a transcription.

## First transcription

1. In **Models**, select **GigaAM v3 RNN-T end-to-end** (`e2e_rnnt`), open **Startup settings**, and click **Deploy**. Wait for **Ready**. The first deployment downloads about 230 MB of model weights.
2. In **Dictation**, upload a short WAV with speech or record a phrase and stop recording. You should see the transcript. Allow the microphone when asked. A browser can record at `http://localhost` on its own computer or over HTTPS with a trusted certificate. On a phone, `localhost` is the phone, not the Docker host; plain HTTP on a LAN address isn't enough. File upload doesn't require microphone permission.
3. For an HTTPS client, open **Client API keys**, name the client and select **Create key**. Copy the key immediately; it's shown once. Use Base URL `https://stt.example.org/v1` (with your own name), model `kamerton`, and the key as `Authorization: Bearer …`. The key can be revoked separately and cannot manage the console. For local mode the Base URL is `http://localhost:8091/v1`; use `API_KEY` from `.env` if set (otherwise no key is needed; use any nonempty dummy value only if the client insists).

To test the HTTPS client yourself, put `recording.wav` in the current directory. The prompt doesn't echo the key:

```bash
read -r -p 'Base URL (including /v1): ' base_url
read -r -s -p 'Client API key: ' STT_API_KEY; printf '\n'
curl --fail-with-body "${base_url%/}/audio/transcriptions" \
  --header "Authorization: Bearer $STT_API_KEY" \
  --form-string 'model=kamerton' --form 'file=@recording.wav'
unset STT_API_KEY
```

**Ready when:** the console or client returns text from your recording. `kamerton` is a stable ID; the active model is changed in the console, not by changing the client model field.

## DNS and certificates

- **LAN or VPN only:** publish the name in your internal DNS pointing at the TLS proxy's reachable address. For split DNS, return the LAN address inside and the public proxy address outside for the **same name**. The name in the browser, certificate and `PUBLIC_ORIGIN` must agree.
- **Public access:** the public `A` record points to the proxy's reachable IPv4 address. Add `AAAA` only if the equivalent IPv6 path actually works; the Caddy template binds IPv4 by default. If there's a router/firewall in front, route traffic to the TLS proxy, not to Kamerton's backend. For ordinary ACME validation, the selected challenge must be reachable on port 80 (HTTP-01) or 443 (TLS-ALPN-01).
- **LAN without opening inbound ports:** use a certificate your devices trust. A supported DNS-01 challenge with a public CA can validate a real DNS name without public access to the site. Alternatively, install your private CA's public root on each device. If you specifically want Caddy's internal CA, use the [manual Caddy setup](public-access.md#частые-вопросы) **instead of** the CLI Caddy route; those commands don't operate the CLI project. Test DNS, certificate trust and microphone permission on the device you will use.

## Stop, reinstall, back up and roll back

- `python3 scripts/kamerton.py stop` stops the installation; `start` starts it again. **Stop** in the console stops only the speech engine. `status`, `doctor` and `logs --follow` (same CLI) help diagnose a problem.
- For a reinstall on the same Docker Engine and absolute path, keep `.kamerton/`, the profile env file, `data/`, `models/`, and any Caddy volumes. Reuse the existing owner store. Use `owner reset-password` if you forgot the password. Moving to a different path, Docker Engine or profile is a [cold migration](#migrate-an-existing-stack), not a fresh initialization of live data.
- `python3 scripts/kamerton.py backup` stops the CLI-managed services for the copy and restarts them if they were running. It prints the backup path under `.kamerton/backups/`; it includes configuration and `data.tar` with owner data, settings and any Caddy volumes. Copy it elsewhere and keep it private. Back up models, images and any external proxy configuration and certificates separately if you need them.
- While the stack is healthy, you can run `git fetch --tags` and `python3 scripts/kamerton.py update --version v1.6.0` (substitute the desired compatible tag), or `update --image` with an already available local image. The CLI backs up before replacing the app and tries to return to the previous image if the update fails. To roll back a compatible version, select the previous tag or image the same way. Neither a version change nor `recover` restores the database from backup. If a release changed the data format, stop all services using those files and test the old image with its matching backup in isolation before restoring it to the original path. `recover` resumes interrupted CLI maintenance; it isn't a general restore command.

## Arcane GitOps

Use the repository-relative Compose path that matches the route. For the external proxy, select **`docker-compose.npm.yml`**, a standalone file; **`docker-compose.yml`** by itself is the local profile. **Don't select `docker-compose.public.yml` by itself**: it's a Caddy overlay that needs the base file. Run Caddy from a checkout with the CLI unless your deployment explicitly supplies both files to Compose.

- **Check build source.** A [Git Sync](https://getarcane.app/docs/docker/git-sync) can copy only the Compose YAML into Arcane's project directory. For NPM, make the matching full source available as the build context (including `Dockerfile`, `console/`, and dependencies), or set `KAMERTON_BUILD_CONTEXT` to a Git context pinned to the intended release tag or full commit SHA. The builder must be able to fetch it. Arcane may rename the Compose file in its project directory; don't rely on the source filename being present there.
- **Set the project environment in Arcane.** In single-file mode, set the values from [`.env.npm.example`](../.env.npm.example) in the GitOps environment; Arcane doesn't read `.env.npm` automatically. Use absolute `KAMERTON_DATA_DIR` and `KAMERTON_MODEL_DIR` paths on the Docker host. Don't point `COMPOSE_FILE` or `COMPOSE_ENV_FILES` at repository files that aren't present.
- **Build and verify the intended image.** Set `KAMERTON_IMAGE` to a distinct local tag for that version and pin `KAMERTON_BUILD_CONTEXT` to the matching source. A Git Sync doesn't guarantee a rebuild of an existing local image just because the context changed. If in doubt, build explicitly as below and verify that Arcane uses the same tag.
- **Auto Sync off is not a lock.** It stops scheduled pulls; a manual **Sync from Git** can still replace the YAML and redeploy a running stack. Test in a separate project, with different ports and data/model paths, before syncing an existing installation.

On the Docker host, work in the actual Arcane project directory. Use [the one-file Arcane helper](../scripts/arcane.sh), which needs Bash and Python 3 but no Python packages. If you cloned the repository elsewhere, adjust the script path below. Each command asks for the local Docker context, Compose project name, effective YAML and a private env file with the same settings as Arcane. It refuses `DOCKER_HOST` and remote contexts rather than falling back to a different daemon.

```bash
bash "$HOME/stt-api/scripts/arcane.sh" check
bash "$HOME/stt-api/scripts/arcane.sh" build
```

`check` is read-only: compare its Engine ID, mount paths, port and origin with Arcane's actual configuration. `build` asks for confirmation and builds the image without starting the service. **For an existing stack**, verify the original directories and `owner.sqlite3` are present. Do not create empty replacements or initialize an owner.

**For a new stack only**, create the two verified bind directories on the Docker host (`mkdir -p` with those absolute paths), then run `bash "$HOME/stt-api/scripts/arcane.sh" create-owner`. The helper requires an empty data directory, including hidden files, and prompts for the password without echoing it. It will not convert a stopped local installation into an owner installation. Start the web service **in Arcane**, not through the installer.

## Migrate an existing stack

The installer doesn't import existing Compose stacks or switch an installation's profile. Keep control of the live stack with its current manager while you prepare and test the target separately.

1. **Inventory before changing anything.** Check the running project name, exact Compose/image/ref and env values, actual mount source paths for `data/` and `models/`, ports, network and trusted peer, current domain, certificate and proxy rules. Map them to the target profile. For example, with NPM use the front origin for `PUBLIC_ORIGIN`, but recalculate `TRUSTED_PROXY_IP` for the new proxy-to-backend route. Don't assume relative paths resolve to the old mounts in Arcane.
2. **Back up.** Disable automated deploys and stop the old stack long enough for a consistent copy. Save the actual `data/`, deployment configuration, and any TLS material you manage. Keep the old release and config available for rollback. Validate the resolved target config without printing secrets. Test the transition on a separate project with **copies**, separate port/network, and a matching test origin. Don't manually sync the live project yet.
3. **Switch one instance at a time.** Stop the old service before starting the new service on the original data. Never run two processes on one `data/` directory. Reuse an existing `owner.sqlite3`; don't run `init` or bootstrap a new owner over it. Verify login, microphone and a real client transcription via HTTPS before declaring the switch complete. To revert, stop the new service, restore the matching old configuration and data if needed, then start only the old service and return the proxy to it.

## Troubleshooting

| Symptom | Check first |
|---|---|
| `502 Bad Gateway` | Is Kamerton running? From the proxy's network, check the backend hostname, HTTP scheme, port, bind and firewall. A `503` from Kamerton instead means the speech engine is not ready; check **Service status**. |
| `403` through HTTPS | Verify the actual immediate peer, the exact public `Host` and the single `X-Forwarded-Proto: https` header. If only a browser action fails, check the current origin and sign in again; don't bypass the checks. `401` from `/v1/models` with a key means the key is missing, wrong or revoked. |
| Port already in use | Before the first successful CLI start, choose a free `HOST_PORT` (local) or `KAMERTON_PORT` (NPM) and update the proxy's forward port. After the first start, the CLI rejects port changes: free the port or migrate. If 80/443 already serve HTTPS, use that proxy instead of Caddy. |
| Certificate warning or no microphone | Check DNS from the client, the certificate name and trust chain, then the site's microphone permission. Check stale `AAAA` records and ACME reachability if issuance fails. Don't ignore certificate warnings. |
